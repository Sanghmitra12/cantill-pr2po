"""Chat intake for requesters: understand free text, ask only for what is missing, confirm, create the PR.

Deterministic slot filling (works offline).  If ANTHROPIC_API_KEY is set, an LLM is used as a fallback
extractor for messages the rules could not read.  The LLM never picks an item code: matching always goes
through the item-master matcher, so the assistant cannot invent items.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid

from . import db, llm, service
from .policy import inr

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
UNIT_WORDS = r"nos|no|pcs|pc|pieces?|units?|kgs?|ltrs?|liters?|litres?|mtrs?|meters?|metres?|pairs?|sets?|boxe?s?|rolls?|reams?|cyl(?:inders?)?|jobs?|bkt|brl|mon|qtr|rol"
URGENT_RE = re.compile(r"\b(emergency|breakdown|break-down|line (?:is )?down|machine (?:is )?down|production (?:is )?(?:stopped|halted)|critical|urgent(?:ly)?|asap|immediately|right away)\b", re.I)
NORMAL_RE = re.compile(r"\b(normal|regular|routine|not urgent|no rush|standard)\b", re.I)


# ------------------------------------------------------------------------------------------
# Parsing helpers
# ------------------------------------------------------------------------------------------
def parse_date(text: str, today: dt.date) -> tuple[dt.date | None, str]:
    """Returns (date, matched_text)."""
    low = text.lower()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", low)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(0)
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-/]*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:[\s,\-/]*(\d{4}))?\b", low)
    if not m:
        m2 = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?\b", low)
        if m2:
            d, mon, y = int(m2.group(2)), MONTHS[m2.group(1)], m2.group(3)
            m = None
            try:
                dd = dt.date(int(y) if y else today.year, mon, d)
                if not y and dd < today:
                    dd = dt.date(today.year + 1, mon, d)
                return dd, m2.group(0)
            except ValueError:
                pass
    if m:
        d, mon, y = int(m.group(1)), MONTHS[m.group(2)], m.group(3)
        try:
            dd = dt.date(int(y) if y else today.year, mon, d)
            if not y and dd < today:
                dd = dt.date(today.year + 1, mon, d)
            return dd, m.group(0)
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", low)
    if m:
        d, mon = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else today.year
        y = y + 2000 if y < 100 else y
        try:
            dd = dt.date(y, mon, d)
            if not m.group(3) and dd < today:
                dd = dt.date(y + 1, mon, d)
            return dd, m.group(0)
        except ValueError:
            pass
    if re.search(r"\bday after tomorrow\b", low):
        return today + dt.timedelta(days=2), "day after tomorrow"
    if re.search(r"\btomorrow\b", low):
        return today + dt.timedelta(days=1), "tomorrow"
    if re.search(r"\btoday\b", low):
        return today, "today"
    m = re.search(r"\bin\s+(\d+)\s*(day|week|month)s?\b", low)
    if m:
        n = int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)]
        return today + dt.timedelta(days=n), m.group(0)
    m = re.search(r"\b(next|this|coming)?\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", low)
    if m:
        target = DAYS.index(m.group(2))
        delta = (target - today.weekday()) % 7
        if m.group(1) == "next":
            delta = delta + 7 if delta < 7 and delta != 0 else (7 if delta == 0 else delta)
            # "next Friday" = the Friday of next week
            delta = (target - today.weekday()) % 7 or 7
            delta += 7 if (target - today.weekday()) % 7 != 0 and (today.weekday() < target) else 0
            if today.weekday() < target:
                delta = target - today.weekday() + 7
            else:
                delta = (7 - today.weekday()) + target
        elif delta == 0:
            delta = 7 if m.group(1) else 0
        return today + dt.timedelta(days=delta), m.group(0).strip()
    if re.search(r"\bnext week\b", low):
        return today + dt.timedelta(days=7), "next week"
    if re.search(r"\bend of (?:the )?month\b", low):
        nxt = (today.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return nxt - dt.timedelta(days=1), "end of month"
    return None, ""


def parse_plant(text: str, plants: dict[str, str]) -> str | None:
    low = text.lower()
    hits = []
    for code, name in plants.items():
        keys = {code.lower()} | {w.lower() for w in re.findall(r"[A-Za-z]{4,}", name) if w.lower() not in ("plant",)}
        if any(re.search(rf"\b{re.escape(k)}\b", low) for k in keys):
            hits.append(code)
    return hits[0] if len(hits) == 1 else None


def parse_quantity(text: str, asked: bool = False) -> tuple[float | None, str, str]:
    """Returns (qty, uom_text, matched_text)."""
    t = text
    if asked:
        m = re.fullmatch(r"\s*(\d[\d,]*(?:\.\d+)?)\s*(" + UNIT_WORDS + r")?\.?\s*", t, re.I)
        if m:
            return float(m.group(1).replace(",", "")), (m.group(2) or ""), m.group(0)
    m = re.search(r"(?:need|require[sd]?|want|order|buy|get|raise|qty|quantity|purchase|pr for|send|arrange)\s*(?:of|for|about|around|approx\.?)?\s*(\d[\d,]*(?:\.\d+)?)\s*(" + UNIT_WORDS + r")?\b", t, re.I)
    if m:
        return float(m.group(1).replace(",", "")), (m.group(2) or ""), m.group(0)
    m = re.search(r"(?<![\w\-x/.])(\d[\d,]*(?:\.\d+)?)\s*(" + UNIT_WORDS + r")\b(?!\s*[-x/])", t, re.I)
    if m:
        return float(m.group(1).replace(",", "")), m.group(2), m.group(0)
    m = re.match(r"\s*(\d[\d,]*(?:\.\d+)?)\s+(?=[A-Za-z])", t)
    if m:
        return float(m.group(1).replace(",", "")), "", m.group(1)
    return None, "", ""


def strip_for_item(text: str, matched: list[str]) -> str:
    s = text
    for m in matched:
        if m:
            s = s.replace(m, " ", 1)
    s = re.sub(r"\b(i|we)\b\s*", " ", s, flags=re.I)
    s = re.sub(r"\b(need|require[sd]?|want|please|pls|raise|a|an|pr|purchase|requisition|request|buy|order|get|for|at|in|to|by|from|of|the|plant|our|us|me|some|urgent(?:ly)?|asap|emergency|normal|regular|deliver(?:y|ed)?|before|on|need-by|needed|required|date|within|qty|quantity|is|are|it|and|with|\.|,)\b", " ", s, flags=re.I)
    s = re.sub(r"[^\w\s\-/.\"x]", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -.,")


# ------------------------------------------------------------------------------------------
# Session handling
# ------------------------------------------------------------------------------------------
def new_state() -> dict:
    return {"stage": "collect", "asked": None, "slots": {}, "candidates": [], "item_text": ""}


def load_session(sid: str | None, user: dict) -> tuple[str, dict]:
    if sid:
        r = db.q1("SELECT * FROM chat_sessions WHERE id=?", (sid,))
        if r:
            return sid, json.loads(r["state"])
    sid = uuid.uuid4().hex[:12]
    st = new_state()
    db.ex("INSERT INTO chat_sessions(id,user,state,created_at,updated_at) VALUES(?,?,?,?,?)", (sid, user["name"], db.jdump(st), service.now(), service.now()))
    return sid, st


def save_session(sid: str, st: dict) -> None:
    db.ex("UPDATE chat_sessions SET state=?, updated_at=? WHERE id=?", (db.jdump(st), service.now(), sid))


def log_msg(sid: str, role: str, content: str) -> None:
    db.ex("INSERT INTO chat_messages(session_id,role,content,ts) VALUES(?,?,?,?)", (sid, role, content, service.now()))


def history(sid: str) -> list[dict]:
    return db.q("SELECT role, content, ts FROM chat_messages WHERE session_id=? ORDER BY id", (sid,))


GREETING = ("Hi! Tell me what you need, e.g. “need 200 nitrile gloves for Chakan by next Friday”. "
            "I'll match the item, ask for anything missing, confirm, and raise the PR.")


def _summary(st: dict, ref) -> str:
    s = st["slots"]
    if s.get("is_new"):
        item = f"NEW ITEM: {s['description']} (not in item master → goes to sourcing)"
    else:
        it = ref.items[s["item_code"]]
        item = f"{s['item_code']} · {it['description']}"
    uom = s.get("uom") or ""
    lines = [f"Item: {item}", f"Quantity: {s['quantity']:g} {uom}", f"Plant: {s['plant']} ({ref.plants.get(s['plant'], '')})",
             f"Need by: {s['need_by']}", f"Urgency: {s['urgency']}"]
    if s.get("is_new"):
        lines.append("Estimated unit price: " + (inr(s["estimated_unit_price"], 2) if s.get("estimated_unit_price") else "not given"))
    return "\n".join(lines)


def _candidates_text(cands: list[dict]) -> str:
    return "\n".join(f"{i}. {c['item_code']} · {c['description']}" for i, c in enumerate(cands, 1))


def handle(session_id: str | None, user: dict, message: str) -> dict:
    sid, st = load_session(session_id, user)
    ref, matcher = service.get_reference()
    message = (message or "").strip()
    if not message:
        return {"session_id": sid, "reply": GREETING, "state": st, "quick_replies": [], "history": history(sid)}
    log_msg(sid, "user", message)
    resp = _step(sid, st, message, user, ref, matcher)
    log_msg(sid, "assistant", resp["reply"])
    save_session(sid, st)
    resp.update(session_id=sid, state=st, history=history(sid))
    return resp


def _reply(text: str, quick: list[str] | None = None, created: dict | None = None) -> dict:
    return {"reply": text, "quick_replies": quick or [], "created": created}


def _step(sid: str, st: dict, text: str, user: dict, ref, matcher) -> dict:
    low = text.lower().strip()
    today = dt.date.today()
    slots = st["slots"]

    # ---- global commands -------------------------------------------------------------------
    if re.fullmatch(r"(cancel|reset|start over|new request|never ?mind|clear)\.?", low):
        st.update(new_state())
        return _reply("Okay, cleared. What do you need?")
    if re.search(r"\b(my (?:requests|prs)|show (?:my )?(?:requests|prs)|status)\b", low) and not slots.get("item_code") and not slots.get("is_new") and st["stage"] == "collect":
        rows = db.q("SELECT pr_no, description, quantity, plant, status FROM pr_lines WHERE source='chat' AND requester=? ORDER BY id DESC LIMIT 8", (user["name"],))
        if not rows:
            return _reply("You have no requests yet. Tell me what you need.")
        return _reply("Your latest requests:\n" + "\n".join(f"• {r['pr_no']} · {r['description']} × {r['quantity']:g} · {r['plant']} · {'actioned' if r['status'] == 'done' else 'with buyer'}" for r in rows))

    # ---- confirmation stage ------------------------------------------------------------------
    if st["stage"] == "confirm":
        if re.fullmatch(r"(y|yes|yep|yeah|ok|okay|confirm|confirmed|correct|go ahead|submit|sure|right|done|looks good|proceed)[\s.!]*", low):
            return _create(st, user, ref)
        if re.fullmatch(r"(n|no|nope|cancel|stop)[\s.!]*", low):
            st.update(new_state())
            return _reply("No problem, I dropped that request. What do you need?")
        # otherwise treat as a correction: fall through to slot extraction below, then re-confirm
        st["stage"] = "collect"

    # ---- candidate pick ----------------------------------------------------------------------
    if st["asked"] == "pick":
        pick = _pick_candidate(low, st["candidates"])
        if pick == "new":
            slots.update(is_new=True, description=st["item_text"], item_code=None)
            st["asked"] = None
            st["candidates"] = []
        elif pick:
            slots.update(item_code=pick, is_new=False, description=ref.items[pick]["description"], uom=ref.items[pick]["uom"])
            st["asked"] = None
            st["candidates"] = []
        else:
            # the user may have re-described the item instead
            st["asked"] = None
            st["item_text"] = ""

    # ---- extract slots from this message -------------------------------------------------------
    got = _extract(text, st, today, ref, matcher)

    # new-item price question
    if st["asked"] == "price":
        m = re.search(r"(\d[\d,]*(?:\.\d+)?)", text)
        if m and not re.search(r"\b(skip|unknown|don'?t know|no idea|not sure)\b", low):
            slots["estimated_unit_price"] = float(m.group(1).replace(",", ""))
        elif re.search(r"\b(skip|unknown|don'?t know|no idea|not sure)\b", low) or got:
            slots["estimated_unit_price"] = None
            slots["price_skipped"] = True
        st["asked"] = None
    # ---- decide the next question --------------------------------------------------------------
    return _next(st, user, ref, got)


def _pick_candidate(low: str, cands: list[dict]) -> str | None:
    if re.search(r"\b(new item|none of (?:these|them)|not (?:in )?(?:the )?list|something else|other)\b", low):
        return "new"
    m = re.fullmatch(r"\s*#?(\d)\s*[.)]?\s*", low)
    if m and 1 <= int(m.group(1)) <= len(cands):
        return cands[int(m.group(1)) - 1]["item_code"]
    m = re.search(r"itm-\d{4}", low)
    if m:
        for c in cands:
            if c["item_code"].lower() == m.group(0):
                return c["item_code"]
    for c in cands:
        if c["description"].lower() in low:
            return c["item_code"]
    # distinguishing words: "nitrile", "cotton"
    best = [c for c in cands if all(w in c["description"].lower() for w in re.findall(r"[a-z0-9]{3,}", low)) and re.findall(r"[a-z0-9]{3,}", low)]
    if len(best) == 1:
        return best[0]["item_code"]
    return None


def _extract(text: str, st: dict, today: dt.date, ref, matcher) -> dict:
    slots = st["slots"]
    got: dict = {}
    matched_bits: list[str] = []
    asked = st["asked"]

    plant = parse_plant(text, ref.plants)
    if plant:
        slots["plant"] = plant
        got["plant"] = plant
        for w in re.findall(r"[A-Za-z0-9]{4,}", " ".join([ref.plants[plant], plant])):
            m = re.search(rf"\b{re.escape(w)}\b", text, re.I)
            if m:
                matched_bits.append(m.group(0))
    d, dtext = parse_date(text, today)
    if d:
        if d < today:
            got["date_past"] = dtext
        else:
            slots["need_by"] = d.isoformat()
            got["need_by"] = d.isoformat()
            matched_bits.append(dtext)
    qty, uom_text, qtext = parse_quantity(text, asked=(asked == "quantity"))
    if qty is not None and asked != "price":
        if asked == "need_by" and not d:
            qty = None
        if qty is not None and not (d and qtext and qtext in dtext):
            if qty <= 0:
                got["bad_qty"] = qty
            else:
                slots["quantity"] = qty
                got["quantity"] = qty
                matched_bits.append(qtext)
    if URGENT_RE.search(text):
        slots["urgency"] = "Emergency"
        got["urgency"] = "Emergency"
    elif NORMAL_RE.search(text) or (asked == "urgency" and re.fullmatch(r"\s*(no|nope|n)\s*[.!]*", text.lower())):
        slots["urgency"] = "Normal"
        got["urgency"] = "Normal"
    elif asked == "urgency" and re.fullmatch(r"\s*(yes|yeah|y|yep)\s*[.!]*", text.lower()):
        slots["urgency"] = "Emergency"
        got["urgency"] = "Emergency"
    # item
    if asked in (None, "item") or not (slots.get("item_code") or slots.get("is_new")):
        item_text = strip_for_item(text, matched_bits) if asked != "quantity" and asked != "need_by" and asked != "plant" else ""
        if asked == "item":
            item_text = item_text or text
        if item_text and len(re.sub(r"[^a-z0-9]", "", item_text.lower())) >= 3 and not (slots.get("item_code") and asked is None and not _looks_like_item_change(item_text, matcher)):
            _resolve_item(st, item_text, ref, matcher, got)
    # LLM fallback if nothing was understood
    if not got and llm.available():
        try:
            js = llm.extract_json(llm.complete(
                "Extract purchase-request fields from the user's message. Reply ONLY with JSON: "
                '{"item_text": string|null, "quantity": number|null, "plant": "PUN1"|"CHK1"|null, "need_by_date": "YYYY-MM-DD"|null, "urgency": "Normal"|"Emergency"|null}. '
                f"Today is {today.isoformat()}. Plants: PUN1=Pune/Bhosari, CHK1=Chakan. Do not guess missing values.", text, max_tokens=300, timeout=20))
            if js.get("quantity"):
                slots["quantity"] = float(js["quantity"]); got["quantity"] = slots["quantity"]
            if js.get("plant") in ref.plants:
                slots["plant"] = js["plant"]; got["plant"] = js["plant"]
            if js.get("need_by_date"):
                slots["need_by"] = js["need_by_date"]; got["need_by"] = js["need_by_date"]
            if js.get("urgency") in ("Normal", "Emergency"):
                slots["urgency"] = js["urgency"]; got["urgency"] = js["urgency"]
            if js.get("item_text"):
                _resolve_item(st, js["item_text"], ref, matcher, got)
        except Exception:  # noqa: BLE001
            pass
    return got


def _looks_like_item_change(item_text: str, matcher) -> bool:
    """Once an item is chosen, only re-resolve if the new text clearly names an item."""
    r = matcher.match(item_text)
    return r.status in ("matched", "exact_code")


def _resolve_item(st: dict, item_text: str, ref, matcher, got: dict) -> None:
    slots = st["slots"]
    m = matcher.match(item_text)
    st["item_text"] = item_text
    if m.status in ("matched", "exact_code"):
        it = ref.items[m.item_code]
        slots.update(item_code=m.item_code, is_new=False, description=it["description"], uom=it["uom"])
        st["candidates"] = []
        got["item"] = m.item_code
    else:
        slots.pop("item_code", None)
        slots.pop("is_new", None)
        st["candidates"] = [{"item_code": c.item_code, "description": c.description, "score": c.score} for c in m.candidates[:4]]
        got["item_unresolved"] = item_text


def _next(st: dict, user: dict, ref, got: dict) -> dict:
    slots = st["slots"]
    pre = ""
    if got.get("date_past"):
        pre += f"“{got['date_past']}” is in the past, so I ignored it. "
    if got.get("bad_qty") is not None:
        pre += "Quantity must be more than zero. "
    # 1. item
    if not slots.get("item_code") and not slots.get("is_new"):
        if got.get("item_unresolved") or st["candidates"]:
            cands = st["candidates"]
            if cands:
                st["asked"] = "pick"
                return _reply(pre + f"I'm not sure which item “{st['item_text']}” is. I won't guess — which one do you mean?\n{_candidates_text(cands)}\nReply with a number, or say “new item” if it isn't listed.",
                              [str(i) for i in range(1, len(cands) + 1)] + ["new item"])
            st["asked"] = "item"
            return _reply(pre + f"I couldn't find anything like “{st['item_text']}” in the item master. Can you describe it differently (type, size, model)? Or say “new item” and I'll raise it as a new item for sourcing.", ["new item"])
        st["asked"] = "item"
        return _reply(pre + "What do you need? (item name, size/model)")
    # 2. new-item details
    if slots.get("is_new") and "estimated_unit_price" not in slots and not slots.get("price_skipped"):
        st["asked"] = "price"
        return _reply(pre + f"“{slots.get('description')}” isn't in the item master, so it will go to sourcing as a new item. What is your estimated unit price in ₹? (or say “skip”)", ["skip"])
    # 3. quantity
    if not slots.get("quantity"):
        st["asked"] = "quantity"
        return _reply(pre + f"How many {slots.get('uom') or 'units'} of {slots.get('description')} do you need?")
    # 4. plant
    if not slots.get("plant"):
        st["asked"] = "plant"
        return _reply(pre + "Which plant is this for?", list(ref.plants) + [n.split(' - ')[-1] for n in ref.plants.values()][:0])
    # 5. need-by
    if not slots.get("need_by"):
        st["asked"] = "need_by"
        return _reply(pre + "When do you need it by? (e.g. “25 Nov”, “next Friday”, “in 10 days”)")
    # 6. urgency
    if not slots.get("urgency"):
        st["asked"] = "urgency"
        return _reply(pre + "Is this an Emergency (production impact) or Normal?", ["Normal", "Emergency"])
    # 7. confirm
    st["stage"] = "confirm"
    st["asked"] = "confirm"
    note = ""
    if slots["urgency"] == "Emergency":
        note = "\nNote: Emergency is for real production impact; it lets Procurement buy without quotes up to a limit, so it is audited."
    return _reply(pre + "Please confirm this purchase request:\n" + _summary(st, ref) + note + "\n\nShall I raise it? (yes / no, or tell me what to change)", ["Yes, raise it", "No, cancel"])


def _create(st: dict, user: dict, ref) -> dict:
    s = st["slots"]
    now = dt.datetime.now().replace(second=0, microsecond=0)
    line = {
        "created_at": now.strftime("%Y-%m-%d %H:%M"), "plant": s["plant"], "item_code": None if s.get("is_new") else s["item_code"],
        "description": s["description"], "quantity": s["quantity"], "uom": s.get("uom") or "NOS", "estimated_unit_price": s.get("estimated_unit_price"),
        "need_by_date": s["need_by"], "urgency": s["urgency"], "department": "",
    }
    out = service.add_chat_line(line, user)
    req = out["requirement"] or {}
    dl = out["decision_line"] or {}
    parts = [f"✅ {out['pr_no']} raised."]
    if dl.get("route") == "NOT_PROCESSED":
        parts.append("Procurement could not process it: " + dl.get("reason", ""))
    elif req:
        club = len(req["line_keys"]) > 1
        if club:
            others = ", ".join(k[0] for k in req["line_keys"] if k[0] != out["pr_no"])
            parts.append(f"It was clubbed with {others} as one requirement ({req['group_id']}, total value {inr(req['value'])}) because the policy treats similar PRs as one requirement.")
        if req["decision"] == "PO":
            parts.append(f"Proposed route: {req['route']} to {req['supplier_id']} at {inr(req['unit_price'], 2)} per unit (requirement value {inr(req['value'])}). A buyer will approve the PO.")
        else:
            sup = f" Suggested suppliers: {', '.join(req['suggested_supplier_ids'])}." if req["suggested_supplier_ids"] else ""
            parts.append(f"Proposed route: {req['route']} (requirement value {inr(req['value'])}).{sup} A buyer will start the sourcing event.")
        parts.append(f"Why: clause {req['clause']}, policy v{req['policy_version']}.")
    st.update(new_state())
    return _reply("\n".join(parts) + "\n\nAnything else?", ["Show my requests"], created={"pr_no": out["pr_no"], "requirement": req})
