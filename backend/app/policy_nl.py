"""Turn a policy amendment (uploaded document text or a chat message) into a new policy version.

Two interpreters, same output contract:
  * LLM (if ANTHROPIC_API_KEY is set): sees the current policy JSON + schema, returns the full amended JSON.
  * Rules (always available): regex/keyword extraction of limit changes, window changes, removals and new
    conditional rules ("at CHK1, Electrical Spares above 2 lakh go to RFQ").
Whatever the interpreter returns is validated, diffed against the base and shown to the user in plain
language; nothing is active until a Procurement user approves it.
"""
from __future__ import annotations

import copy
import datetime as dt
import io
import json
import re

from . import llm, policy as P

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# ------------------------------------------------------------------------------------------
# Text extraction from uploads
# ------------------------------------------------------------------------------------------
def extract_text(name: str, data: bytes) -> str:
    low = name.lower()
    if low.endswith(".pdf"):
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    if low.endswith(".docx"):
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf8", "ignore")
        xml = re.sub(r"</w:p>", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml)
    return data.decode("utf-8-sig", errors="replace")


# ------------------------------------------------------------------------------------------
# Small parsers
# ------------------------------------------------------------------------------------------
UNIT = {"crore": 1e7, "crores": 1e7, "cr": 1e7, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "l": 1e5, "k": 1e3, "thousand": 1e3}
MONEY_RE = re.compile(r"(₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?)\s*(crores?|cr|lakhs?|lacs?|lakh|thousand|l|k)?(?![\w])", re.I)


def find_money(text: str) -> list[tuple[float, int, int]]:
    out = []
    for m in MONEY_RE.finditer(text):
        cur, num, unit = m.group(1), m.group(2), m.group(3)
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        if unit:
            v *= UNIT[unit.lower()]
        elif not cur and v < 10000:
            continue  # bare small numbers are days/hours/counts, not money
        elif not cur and re.match(r"\s*(days?|hours?|hrs?|%|suppliers?)", text[m.end():], re.I):
            continue
        out.append((v, m.start(), m.end()))
    return out


def find_effective_date(text: str) -> str | None:
    pats = [
        r"(?:effective|w\.?e\.?f\.?|with effect from|applicable from|from)\s*(?:date)?\s*[:\-]?\s*(\d{4}-\d{2}-\d{2})",
        r"(?:effective|w\.?e\.?f\.?|with effect from|applicable from|from)\s*(?:date)?\s*[:\-]?\s*(\d{1,2})(?:st|nd|rd|th)?[\s\-/]+([A-Za-z]{3,9})\.?,?[\s\-/]+(\d{4})",
        r"(?:effective|w\.?e\.?f\.?|with effect from|applicable from|from)\s*(?:date)?\s*[:\-]?\s*([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
        r"(?:effective|w\.?e\.?f\.?|with effect from|applicable from|from)\s*(?:date)?\s*[:\-]?\s*(\d{1,2})[/-](\d{1,2})[/-](\d{4})",
    ]
    for i, p in enumerate(pats):
        m = re.search(p, text, re.I)
        if not m:
            continue
        g = m.groups()
        try:
            if i == 0:
                return dt.date.fromisoformat(g[0]).isoformat()
            if i == 1 and g[1][:3].lower() in MONTHS:
                return dt.date(int(g[2]), MONTHS[g[1][:3].lower()], int(g[0])).isoformat()
            if i == 2 and g[0][:3].lower() in MONTHS:
                return dt.date(int(g[2]), MONTHS[g[0][:3].lower()], int(g[1])).isoformat()
            if i == 3:
                return dt.date(int(g[2]), int(g[1]), int(g[0])).isoformat()
        except ValueError:
            continue
    return None


def find_version(text: str) -> str | None:
    m = re.search(r"\bversion\s*(?:no\.?)?\s*[:\-]?\s*v?(\d+\.\d+)", text, re.I) or re.search(r"\bv(\d+\.\d+)\b", text, re.I)
    return m.group(1) if m else None


# ------------------------------------------------------------------------------------------
# Rule-based interpreter
# ------------------------------------------------------------------------------------------
ROUTE_WORDS = [
    (r"emergency\s+po", "EMERGENCY_PO"), (r"spot\s+po|direct\s+po|direct\s+purchase|place\s+a\s+po|purchase\s+order\s+(?:may|can)\s+be\s+placed", "SPOT_PO"),
    (r"contract\s+po|rate\s+contract", "CONTRACT_PO"), (r"capex\s+sourcing", "CAPEX_SOURCING"),
    (r"\brfp\b|request\s+for\s+proposal", "RFP"), (r"\brfq\b|request\s+for\s+quot|quotes?\b|quotation|sourcing", "RFQ"),
]
SPLIT_RE = re.compile(r"(?:\n+|(?<=[\.;])\s+(?=[A-Z0-9])|;\s+|\s+•\s+)")


CONCESSION_RE = re.compile(r"(?:even if|despite|regardless of|irrespective of|notwithstanding|whether or not|overriding)\s+(?:a|an|the|any)?\s*(?:valid\s+|active\s+)?(?:rate\s+)?contracts?[^,.;]*", re.I)


def _route_of(sentence: str) -> str | None:
    sentence = CONCESSION_RE.sub(" ", sentence)
    for pat, route in ROUTE_WORDS:
        if re.search(pat, sentence, re.I):
            return route
    return None


def _rule_for_route(doc: dict, route: str) -> dict | None:
    for r in doc["rules"]:
        if r["outcome"]["route"] == route:
            return r
    return None


def _value_leaf(cond: dict | None) -> dict | None:
    if not cond:
        return None
    if "field" in cond:
        return cond if cond["field"] == "value" else None
    for k in ("all", "any"):
        if k in cond:
            for s in cond[k]:
                hit = _value_leaf(s)
                if hit:
                    return hit
    return None


def _set_limit(doc: dict, route: str, new_value: float, notes: list[str], op_hint: str | None = None) -> bool:
    rule = _rule_for_route(doc, route)
    if not rule:
        return False
    leaf = _value_leaf(rule.get("when"))
    if not leaf:
        return False
    leaf["value"] = new_value
    if op_hint:
        leaf["op"] = op_hint
    # keep the RFQ/RFP boundary consistent (RFQ: value < X, RFP: value >= X)
    if route in ("RFQ", "RFP"):
        other = _rule_for_route(doc, "RFP" if route == "RFQ" else "RFQ")
        oleaf = _value_leaf(other.get("when")) if other else None
        if oleaf and oleaf["value"] != new_value and oleaf["op"] in ("<", ">="):
            oleaf["value"] = new_value
            notes.append(f"Also moved the {other['outcome']['route']} boundary to {P.inr(new_value)} so RFQ/RFP stay contiguous")
    return True


def parse_conditions(sentence: str, meta: dict) -> list[dict]:
    conds: list[dict] = []
    s = sentence
    low = s.lower()
    # plant
    plants = []
    for code, name in meta.get("plants", {}).items():
        keys = {code.lower()} | {w.lower() for w in re.findall(r"[A-Za-z]{4,}", name) if w.lower() not in ("plant", "pune")}
        if any(re.search(rf"\b{re.escape(k)}\b", low) for k in keys):
            plants.append(code)
    if plants:
        conds.append({"field": "plant", "op": "==", "value": plants[0]} if len(plants) == 1 else {"field": "plant", "op": "in", "value": plants})
    # category
    cats = [c for c in meta.get("categories", []) if re.search(rf"\b{re.escape(c.lower())}\b", low)]
    if not cats:  # allow loose match on first significant word(s), e.g. "electrical spares"
        for c in meta.get("categories", []):
            if c.lower().replace("&", "and") in low.replace("&", "and"):
                cats.append(c)
    if cats:
        conds.append({"field": "category", "op": "==", "value": cats[0]} if len(cats) == 1 else {"field": "category", "op": "in", "value": cats})
    # item type
    for word, val in (("capex", "Capex"), ("capital", "Capex"), ("service", "Service"), ("services", "Service")):
        if re.search(rf"\b{word}\b", low) and not re.search(r"capex sourcing", low):
            if not any(c["field"] == "category" for c in conds) or word in ("capex", "capital"):
                conds.append({"field": "item_type", "op": "==", "value": val})
                break
    # item codes
    codes = re.findall(r"ITM-\d{4}", s, re.I)
    if codes:
        codes = [c.upper() for c in codes]
        conds.append({"field": "item_code", "op": "==", "value": codes[0]} if len(codes) == 1 else {"field": "item_code", "op": "in", "value": codes})
    # emergency
    if re.search(r"emergenc(?:y|ies)\s+(?:pr|requisition|requirement|request|line|purchase)s?|marked\s+(?:as\s+)?emergency|urgent\s+(?:pr|requirement)", low):
        conds.append({"field": "is_emergency", "op": "==", "value": True})
    # value comparisons
    money = find_money(s)
    for v, a, b in money:
        before = low[max(0, a - 40):a]
        after = low[b:b + 25]
        if re.search(r"(at least|minimum of|not less than|or more|≥|>=)\s*$", before) or re.match(r"\s*(or more|and above|or above|\+)", after):
            conds.append({"field": "value", "op": ">=", "value": v})
        elif re.search(r"(above|over|more than|exceed\w*|greater than|higher than|beyond|>)\s*$", before):
            conds.append({"field": "value", "op": ">", "value": v})
        elif re.search(r"(up to|upto|not more than|not exceeding|at most|maximum of|≤|<=)\s*$", before) or re.match(r"\s*(or less|and below)", after):
            conds.append({"field": "value", "op": "<=", "value": v})
        elif re.search(r"(below|under|less than|lower than|<)\s*$", before):
            conds.append({"field": "value", "op": "<", "value": v})
    return conds


def interpret_rules(text: str, base: dict, meta: dict) -> dict:
    doc = copy.deepcopy(P.normalise_policy(base))
    notes: list[str] = []
    unparsed: list[str] = []
    version = find_version(text)
    eff = find_effective_date(text)
    clauses = [c.strip(" -•*\t") for c in SPLIT_RE.split(text) if c and c.strip(" -•*\t")]
    new_rule_n = 0
    handled_any = False
    for sent in clauses:
        low = sent.lower()
        if len(low) < 8 or re.search(r"\bamendment\b.*\bversion\b|^version\b|^effective\b|^document\b", low):
            continue
        done = False
        # ---- parameters (windows) -------------------------------------------------------
        dm = re.findall(r"(\d+)\s*(?:calendar\s*)?days?", low)
        m = re.search(r"(\d+)\s*(?:calendar\s*)?days?(?!.*\d+\s*(?:calendar\s*)?days?)", low) if dm else None
        if m and re.search(r"club|same requirement|splitting|split\s+pr|first line", low) and not re.search(r"need[\s-]*by", low):
            doc["parameters"]["club_window_days"] = int(m.group(1)); done = True
        elif m and re.search(r"need[\s-]*by", low):
            doc["parameters"]["club_need_by_days"] = int(m.group(1)); done = True
        elif m and re.search(r"preferred", low):
            doc["parameters"]["preferred_lookback_days"] = int(m.group(1)); done = True
        elif m and re.search(r"(purchase|buying|bought|history|recently)", low) and re.search(r"look\s*back|last|window|within|period|history", low) and not re.search(r"price", low):
            doc["parameters"]["history_lookback_days"] = int(m.group(1)); done = True
        elif m and re.search(r"price|valuation|most recent po", low):
            doc["parameters"]["price_lookback_days"] = int(m.group(1)); done = True
        m = re.search(r"(\d+)\s*(?:hours?|hrs?)(?!.*\d+\s*(?:hours?|hrs?))", low)
        if m and re.search(r"consolidat|combin|one po|same po", low):
            doc["consolidation"]["window_hours"] = int(m.group(1)); done = True
        if done:
            notes.append(f"Parameter change read from: “{sent[:140]}”")
            handled_any = True
            continue
        # ---- removing a rule ------------------------------------------------------------
        if re.search(r"\b(remove|withdraw|discontinue|abolish|scrap|no longer (?:allowed|permitted|applies|apply)|delete|disable)\b", low):
            route = _route_of(sent) or ("EMERGENCY_PO" if "emergency" in low else None)
            rule = _rule_for_route(doc, route) if route else None
            if rule:
                doc["rules"] = [r for r in doc["rules"] if r["id"] != rule["id"]]
                notes.append(f"Removed rule {rule['clause']} ({rule['name']})")
                handled_any = True
                continue
        # ---- limit change on an existing rule -------------------------------------------
        money = find_money(sent)
        limit_words = re.search(r"\blimit|threshold|ceiling|cap\b|up to|upto|below|under|less than|at or above|above|more than|increase|raise|reduc|lower|revis|chang|set to|now|instead|from .* to", low)
        route_for_limit = None
        if re.search(r"\bspot\b", low):
            route_for_limit = "SPOT_PO"
        elif re.search(r"emergency", low):
            route_for_limit = "EMERGENCY_PO"
        elif re.search(r"\brfp\b|proposal", low):
            route_for_limit = "RFP"
        elif re.search(r"\brfq\b|quot", low):
            route_for_limit = "RFQ"
        cond_probe = parse_conditions(sent, meta)
        scoped = any(c["field"] in ("plant", "category", "item_type", "item_code", "is_emergency") for c in cond_probe) and not re.search(r"\blimit\b.*\b(raised|increased|reduced|lowered|revised|changed)", low)
        mm = re.search(r"to\s+(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?)\s*(crores?|cr|lakhs?|lacs?|lakh|l|k)?", low)
        if route_for_limit and money and limit_words and not scoped:
            newv = money[-1][0]
            if mm:
                v = float(mm.group(1).replace(",", ""))
                v *= UNIT[mm.group(2)] if mm.group(2) else 1
                newv = v
            op = None
            if route_for_limit == "SPOT_PO" or route_for_limit == "RFQ":
                op = "<="if re.search(r"up to|upto|not exceeding|at most|or less|≤|<=", low) else None
            if _set_limit(doc, route_for_limit, newv, notes, op):
                notes.append(f"{route_for_limit} limit set to {P.inr(newv)} (from “{sent[:120]}”)")
                handled_any = True
                continue
        # ---- new conditional rule -------------------------------------------------------
        route = _route_of(sent)
        conds = cond_probe
        if route and conds:
            new_rule_n += 1
            outcome = {
                "SPOT_PO": {"decision": "PO", "route": "SPOT_PO", "supplier": {"mode": "preferred_first"}},
                "EMERGENCY_PO": {"decision": "PO", "route": "EMERGENCY_PO", "supplier": {"mode": "preferred_first"}},
                "CONTRACT_PO": {"decision": "PO", "route": "CONTRACT_PO", "supplier": {"mode": "contract"}},
                "RFQ": {"decision": "SOURCING_REQUEST", "route": "RFQ", "supplier": {"mode": "suggest", "count": 3}},
                "RFP": {"decision": "SOURCING_REQUEST", "route": "RFP", "supplier": {"mode": "suggest", "count": 5}},
                "CAPEX_SOURCING": {"decision": "SOURCING_REQUEST", "route": "CAPEX_SOURCING", "supplier": {"mode": "suggest", "count": 5}},
            }[route]
            mcount = re.search(r"(\d)\s*suppliers?", low)
            if mcount and outcome["decision"] == "SOURCING_REQUEST":
                outcome["supplier"]["count"] = int(mcount.group(1))
            if outcome["decision"] == "PO" and re.search(r"purchase history|bought|previously", low):
                conds.append({"field": "bought_recently", "op": "==", "value": True})
            when = conds[0] if len(conds) == 1 else {"all": conds}
            ver = version or "new"
            rid = re.search(r"(?:clause|rule|section)\s+(\d+(?:\.\d+)?[A-Za-z]?)", sent, re.I)
            rule_id = rid.group(1) if rid and not any(r["id"] == rid.group(1) for r in doc["rules"]) else f"AMD-{ver}-{new_rule_n}"
            rule = {"id": rule_id, "clause": rule_id, "name": sent[:60].rstrip(" .,"), "when": when, "outcome": outcome}
            # placement: capex-scoped rules before 5.1; "regardless of contract" before the contract rule; otherwise after
            # the contract rule and before the generic value rules (first matching rule wins)
            orig = {r["id"] for r in P.normalise_policy(base)["rules"]}  # anchor on the pre-existing rules only
            ids = [r["id"] for r in doc["rules"]]
            if any(c["field"] == "item_type" and c["value"] == "Capex" for c in conds):
                idx = ids.index("5.1") if "5.1" in ids else 0
            elif re.search(r"regardless|even if|despite|irrespective|overrid|notwithstanding|before (?:checking )?contract", low):
                idx = next((i for i, r in enumerate(doc["rules"]) if r["id"] in orig and r["outcome"]["route"] == "CONTRACT_PO"), 0)
            else:
                idx = next((i for i, r in enumerate(doc["rules"]) if r["id"] in orig and r["outcome"]["route"] in ("EMERGENCY_PO", "SPOT_PO", "RFQ", "RFP")), len(doc["rules"]))
            doc["rules"].insert(idx, rule)
            notes.append(f"New rule {rule_id} added from: “{sent[:140]}”")
            handled_any = True
            continue
        if re.search(r"[\d₹]|rule|clause|policy|route|limit|supplier|po\b|rfq|rfp", low) and len(low) > 25 and not re.search(r"^(version|document|effective|owner|applies)", low):
            unparsed.append(sent)
    if version:
        doc["version"] = version
    if eff:
        doc["effective_date"] = eff
    return {"doc": doc, "notes": notes, "unparsed": unparsed, "method": "rules", "handled": handled_any, "version_found": version, "effective_found": eff}


# ------------------------------------------------------------------------------------------
# LLM interpreter
# ------------------------------------------------------------------------------------------
SYSTEM = """You maintain a procurement policy stored as JSON. You receive the CURRENT policy JSON and an AMENDMENT written by Procurement
(a document or a chat message). Return the complete AMENDED policy JSON, changing only what the amendment says.

Schema rules:
- doc.version (string, e.g. "5.1"), doc.effective_date (YYYY-MM-DD, taken from the amendment if stated, else keep the current one).
- doc.parameters: club_window_days, club_need_by_days, price_lookback_days, history_lookback_days, preferred_lookback_days.
- doc.rules: ORDERED list, first matching rule wins. Each rule: {id, clause, name, when, outcome}.
  when = {"all":[...]} | {"any":[...]} | {"not":{...}} | {"field","op","value"}.
  fields: %(fields)s
  ops: ==, !=, <, <=, >, >=, in, not_in, contains.
  outcome = {decision: "PO"|"SOURCING_REQUEST", route: string, supplier: {mode: "contract"|"preferred_first"|"suggest"|"none", count?: int}, notes?: string}.
  PO outcomes use mode contract or preferred_first. Sourcing outcomes use suggest (count 3 for RFQ, 5 for RFP/capex) or none.
- doc.supplier_ranking.weights {frequency, recency, contract, on_time, quality}, doc.consolidation {enabled, window_hours, same[]}.
- Money is INR; "2 lakh" = 200000, "1 crore" = 10000000.
- To add a plant-specific / category-specific / value-specific rule, ADD a rule with the right conditions at the right position in the list (before the rule it must override).
- Keep every existing rule id unless the amendment removes it. New rule ids: use the clause number given in the amendment or "AMD-<version>-<n>".
- Do not invent changes. If part of the amendment cannot be expressed, list it in "unparsed".
Reply with ONLY JSON: {"policy": {...full policy...}, "unparsed": ["..."], "assumptions": ["..."]}"""


def interpret_llm(text: str, base: dict) -> dict:
    system = SYSTEM % {"fields": "; ".join(f"{k} ({v['type']})" for k, v in P.FIELDS.items())}
    user = f"CURRENT POLICY JSON:\n{json.dumps(base, indent=1)}\n\nAMENDMENT:\n{text}\n"
    out = llm.extract_json(llm.complete(system, user))
    doc = P.normalise_policy(out["policy"])
    return {"doc": doc, "notes": out.get("assumptions", []), "unparsed": out.get("unparsed", []), "method": "llm", "handled": True}


# ------------------------------------------------------------------------------------------
# Public entry
# ------------------------------------------------------------------------------------------
def interpret(text: str, base: dict, meta: dict, existing_versions: list[str], prefer_llm: bool = True) -> dict:
    base = P.normalise_policy(base)
    result = None
    err = None
    if prefer_llm and llm.available():
        try:
            result = interpret_llm(text, base)
            errs = P.validate_policy(result["doc"])
            if errs:
                err = "LLM output failed validation: " + "; ".join(errs[:3])
                result = None
        except Exception as e:  # noqa: BLE001
            err = f"LLM unavailable ({type(e).__name__}); used built-in rules"
            result = None
    if result is None:
        result = interpret_rules(text, base, meta)
        if err:
            result["notes"].insert(0, err)
        # explicit dates / versions in the text always win over defaults
    doc = result["doc"]
    if not find_version(text) and doc["version"] == base["version"]:
        doc["version"] = P.next_version(existing_versions)
    if result["method"] == "rules" and not result.get("effective_found"):
        result["notes"].append(f"No effective date found in the text; kept {doc['effective_date']} (edit before approving)")
    result["changes"] = P.diff_policies(base, doc)
    return result
