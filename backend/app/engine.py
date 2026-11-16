"""Decision engine: PR lines -> requirements -> decisions -> consolidated POs.

Pure functions, no database or web imports.  Everything that is a *rule* (limits, windows, rule order,
conditions, outcomes) comes from the policy document; this module only interprets it.
"""
from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass, field

from . import policy as P
from .matching import ItemMatcher
from .suppliers import Reference, preferred_list, suggest_suppliers

NOT_PROCESSED = "NOT_PROCESSED"


# ------------------------------------------------------------------------------------------
# Parsing / normalising PR lines
# ------------------------------------------------------------------------------------------
def parse_dt(v) -> dt.datetime | None:
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, dt.date):
        return dt.datetime.combine(v, dt.time())
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def parse_num(v) -> float | None:
    s = str(v if v is not None else "").strip().replace(",", "")
    if s == "" or s.lower() in ("nan", "none", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def plant_code(raw: str, plants: dict[str, str]) -> str | None:
    s = (raw or "").strip()
    if s.upper() in plants:
        return s.upper()
    low = s.lower()
    hits = [code for code, name in plants.items() if low and (low in name.lower() or low == code.lower())]
    return hits[0] if len(hits) == 1 else None


@dataclass
class Line:
    pr_no: str
    pr_line: int
    created_at: dt.datetime
    plant: str
    item_code: str | None
    description: str
    quantity: float
    uom: str
    est_price: float | None
    need_by: dt.date
    urgency: str
    department: str = ""
    requester: str = ""
    # filled by the engine
    resolved: str | None = None
    match_note: str = ""
    skip_reason: str = ""

    @property
    def sort_key(self):
        return (self.created_at, self.pr_no, self.pr_line)

    @property
    def emergency(self) -> bool:
        return self.urgency.strip().lower() == "emergency"


def lines_from_rows(rows: list[dict], plants: dict[str, str]) -> tuple[list[Line], list[dict]]:
    """Returns (valid-looking lines, skipped rows as decision dicts)."""
    lines, skipped = [], []
    seen: set[tuple] = set()
    for r in rows:
        pr_no = str(r.get("pr_no", "")).strip()
        try:
            pr_line = int(float(str(r.get("pr_line", "1") or "1")))
        except ValueError:
            pr_line = 1
        base = {"pr_no": pr_no, "pr_line": pr_line, "resolved_item_code": (r.get("item_code") or "").strip() or ""}
        why = None
        created = parse_dt(r.get("created_at"))
        qty = parse_num(r.get("quantity"))
        plant = plant_code(str(r.get("plant", "")), plants)
        if (pr_no, pr_line) in seen:
            why = "duplicate PR line (same pr_no and pr_line already in the batch)"
        elif created is None:
            why = f"unreadable created_at '{r.get('created_at')}'"
        elif qty is None or qty <= 0:
            why = f"quantity is {r.get('quantity')!s} - nothing to buy"
        elif plant is None:
            why = f"unknown plant '{r.get('plant')}' (known plants: {', '.join(plants)})"
        if why:
            skipped.append({**base, "reason": why})
            continue
        seen.add((pr_no, pr_line))
        nb = parse_dt(r.get("need_by_date"))
        lines.append(Line(
            pr_no=pr_no, pr_line=pr_line, created_at=created, plant=plant,
            item_code=(r.get("item_code") or "").strip().upper() or None,
            description=str(r.get("description") or "").strip(), quantity=qty,
            uom=str(r.get("uom") or "").strip(), est_price=parse_num(r.get("estimated_unit_price")),
            need_by=(nb.date() if nb else created.date()),
            urgency=str(r.get("urgency") or "Normal").strip() or "Normal",
            department=str(r.get("department") or "").strip(), requester=str(r.get("requester") or "").strip(),
        ))
    return lines, skipped


# ------------------------------------------------------------------------------------------
# Requirements
# ------------------------------------------------------------------------------------------
@dataclass
class Requirement:
    seq: int
    item_code: str | None          # None = new item
    plant: str
    lines: list[Line]
    policy: dict
    first_created: dt.datetime
    first_need_by: dt.date
    group_id: str = ""
    result: dict = field(default_factory=dict)

    @property
    def req_date(self) -> dt.date:
        return self.first_created.date()

    @property
    def quantity(self) -> float:
        return sum(l.quantity for l in self.lines)

    @property
    def emergency(self) -> bool:
        return any(l.emergency for l in self.lines)

    @property
    def sort_key(self):
        f = min(self.lines, key=lambda l: l.sort_key)
        return f.sort_key


def _club(lines: list[Line], policies: list[dict]) -> list[Requirement]:
    reqs: list[Requirement] = []
    open_by_key: dict[tuple, list[Requirement]] = {}
    for l in sorted(lines, key=lambda l: l.sort_key):
        pol = P.version_for_date(policies, l.created_at.date())
        if l.resolved is None:  # new item: never clubbed (1.2)
            reqs.append(Requirement(len(reqs) + 1, None, l.plant, [l], pol, l.created_at, l.need_by))
            continue
        key = (l.resolved, l.plant)
        joined = None
        for r in open_by_key.get(key, []):  # earliest requirement first
            win, tol = r.policy["parameters"]["club_window_days"], r.policy["parameters"]["club_need_by_days"]
            if (l.created_at.date() - r.first_created.date()).days <= win and abs((l.need_by - r.first_need_by).days) <= tol:
                joined = r
                break
        if joined:
            joined.lines.append(l)
        else:
            r = Requirement(len(reqs) + 1, l.resolved, l.plant, [l], pol, l.created_at, l.need_by)
            reqs.append(r)
            open_by_key.setdefault(key, []).append(r)
    for r in reqs:
        r.group_id = f"REQ-{r.seq:04d}"
    return reqs


# ------------------------------------------------------------------------------------------
# Valuation / contracts / facts
# ------------------------------------------------------------------------------------------
def _applicable_contracts(ref: Reference, item: str, on: dt.date, pol: dict) -> list[dict]:
    out = []
    for c in ref.contracts_for_item(item):
        if c["start_date"] <= on <= c["end_date"] and ref.supplier_ok(c["supplier_id"], pol):
            out.append(c)
    return out


def _contract_assessment(ref: Reference, req: Requirement, consumed: dict[str, float]) -> dict:
    """Which contracts apply, which of them have room, which one would be used (4.1-4.3)."""
    pol = req.policy
    info = {"applicable": [], "usable": None, "notes": []}
    if req.item_code is None:
        return info
    for c in _applicable_contracts(ref, req.item_code, req.req_date, pol):
        rate = c["lines"][req.item_code]
        cost = req.quantity * rate
        used = consumed.get(c["contract_id"], c["consumed_value"])
        left = c["value_cap"] - used
        ok = used + cost <= c["value_cap"] + 1e-9
        info["applicable"].append({"contract_id": c["contract_id"], "supplier_id": c["supplier_id"], "rate": rate,
                                   "cost": round(cost, 2), "ceiling": c["value_cap"], "consumed": round(used, 2),
                                   "remaining": round(left, 2), "has_room": ok})
    # also report contracts that exist but cannot apply, to make the 'why' complete
    for c in ref.contracts_for_item(req.item_code):
        if any(a["contract_id"] == c["contract_id"] for a in info["applicable"]):
            continue
        if not (c["start_date"] <= req.req_date <= c["end_date"]):
            info["notes"].append(f"{c['contract_id']} not valid on {req.req_date} ({c['start_date']} to {c['end_date']})")
        elif not ref.supplier_ok(c["supplier_id"], pol):
            st = ref.suppliers.get(c["supplier_id"], {}).get("status", "unknown")
            info["notes"].append(f"{c['contract_id']} ignored: supplier {c['supplier_id']} is {st}")
    usable = [a for a in info["applicable"] if a["has_room"]]
    if usable:
        info["usable"] = min(usable, key=lambda a: (a["rate"], a["contract_id"]))  # 4.3 lowest rate with room
    return info


def _valuation(ref: Reference, req: Requirement, ca: dict) -> tuple[float, float, dict]:
    """3.1: returns (unit_price, value, source)."""
    pol = req.policy
    if req.item_code is None:
        l = req.lines[0]
        up = l.est_price or 0.0
        return up, round(req.quantity * up, 2), {"type": "ESTIMATE", "text": "requester's estimated unit price" if l.est_price else "no estimate given"}
    if ca["usable"]:
        a = ca["usable"]
        return a["rate"], round(req.quantity * a["rate"], 2), {"type": "CONTRACT", "contract_id": a["contract_id"], "text": f"rate of contract {a['contract_id']}"}
    if ca["applicable"]:
        a = min(ca["applicable"], key=lambda a: (a["rate"], a["contract_id"]))
        return a["rate"], round(req.quantity * a["rate"], 2), {"type": "CONTRACT_EXHAUSTED", "contract_id": a["contract_id"], "text": f"lowest applicable contract rate ({a['contract_id']}; every applicable contract is out of ceiling)"}
    pos = ref.pos_in_window(req.item_code, req.req_date, pol["parameters"]["price_lookback_days"])
    if pos:
        p = pos[-1]
        return p["unit_price"], round(req.quantity * p["unit_price"], 2), {"type": "PO", "po_number": p["po_number"], "text": f"most recent PO {p['po_number']} ({p['po_date']}, {p['supplier_id']}) within {pol['parameters']['price_lookback_days']} days"}
    sp = float(ref.items[req.item_code]["standard_price"])
    return sp, round(req.quantity * sp, 2), {"type": "STANDARD", "text": "item master standard price (no PO in lookback window)"}


def _facts(ref: Reference, req: Requirement, ca: dict, unit_price: float, value: float, pref: list[dict], bought: list[dict]) -> dict:
    it = ref.items.get(req.item_code) if req.item_code else None
    first = min(req.lines, key=lambda l: l.sort_key)
    return {
        "value": value, "quantity": req.quantity, "unit_price": unit_price,
        "item_type": it["item_type"] if it else None, "category": it["category"] if it else None,
        "item_code": req.item_code, "plant": req.plant,
        "department": sorted({l.department for l in req.lines if l.department}),
        "urgency": "Emergency" if req.emergency else "Normal", "is_emergency": req.emergency,
        "item_is_new": req.item_code is None,
        "contract_usable": ca["usable"] is not None, "contract_applicable": bool(ca["applicable"]),
        "bought_recently": bool(bought), "purchase_count": len(bought),
        "preferred_supplier_count": len(pref), "line_count": len(req.lines),
        "need_by_days": (first.need_by - req.req_date).days,
    }


# ------------------------------------------------------------------------------------------
# Deciding one requirement
# ------------------------------------------------------------------------------------------
def _decide(ref: Reference, req: Requirement, consumed: dict[str, float]) -> dict:
    pol = req.policy
    ca = _contract_assessment(ref, req, consumed)
    unit_price, value, vsrc = _valuation(ref, req, ca)
    pref = preferred_list(ref, req.item_code, req.req_date, pol) if req.item_code else []
    bought = []
    if req.item_code:
        bought = [p for p in ref.pos_in_window(req.item_code, req.req_date, pol["parameters"]["history_lookback_days"]) if ref.supplier_ok(p["supplier_id"], pol)]
    facts = _facts(ref, req, ca, unit_price, value, pref, bought)

    skipped_notes: list[str] = []
    chosen_rule, outcome = None, None
    for rule in pol["rules"]:
        if not P.evaluate(rule.get("when"), facts):
            continue
        mode = (rule["outcome"].get("supplier") or {}).get("mode")
        if mode == "contract" and not ca["usable"]:
            skipped_notes.append(f"rule {rule['clause']} matched but no usable contract; skipped")
            continue
        if mode == "preferred_first" and not pref:
            skipped_notes.append(f"rule {rule['clause']} matched but the item has no eligible preferred supplier; skipped")
            continue
        chosen_rule, outcome = rule, rule["outcome"]
        break
    if chosen_rule is None:
        outcome = pol["fallback_outcome"]
        chosen_rule = {"id": "fallback", "clause": "fallback", "name": "No rule matched", "when": None, "outcome": outcome}
        skipped_notes.append("no rule matched; the policy's fallback outcome applied")

    res = {
        "group_id": req.group_id, "item_code": req.item_code or "NEW", "plant": req.plant,
        "requirement_date": req.req_date.isoformat(), "first_created_at": req.first_created.isoformat(sep=" ", timespec="minutes"),
        "quantity": req.quantity, "uom": req.lines[0].uom, "unit_price_basis": round(unit_price, 4),
        "value": value, "valuation_source": vsrc, "is_emergency": req.emergency,
        "policy_version": pol["version"], "clause": chosen_rule["clause"], "rule_name": chosen_rule.get("name", ""),
        "decision": outcome["decision"], "route": outcome["route"],
        "supplier_id": None, "unit_price": None, "suggested_supplier_ids": [], "suggestions_detail": [],
        "contract_id": None, "reference_po": None, "notes": outcome.get("notes", ""),
        "facts": facts, "contract_assessment": ca, "skipped_rules": skipped_notes,
        "preferred_top": [{k: p[k] for k in ("rank", "supplier_id", "supplier_name", "score", "po_count", "recency", "had_contract", "on_time_pct", "quality", "reference_price", "breakdown", "last_po_date")} for p in pref[:5]],
        "line_keys": [[l.pr_no, l.pr_line] for l in req.lines],
        "need_by": min(l.need_by for l in req.lines).isoformat(), "plant_name": ref.plants.get(req.plant, ""),
        "item_description": ref.items[req.item_code]["description"] if req.item_code else req.lines[0].description,
        "category": facts["category"], "item_type": facts["item_type"], "po_id": None,
    }
    mode = (outcome.get("supplier") or {}).get("mode")
    why = P.condition_trace(chosen_rule.get("when"), facts) if chosen_rule.get("when") else []
    clause_txt = f"Clause {chosen_rule['clause']} ({chosen_rule.get('name', '')})"
    if mode == "contract":
        a = ca["usable"]
        res.update(supplier_id=a["supplier_id"], unit_price=round(a["rate"], 2), contract_id=a["contract_id"])
        consumed[a["contract_id"]] = consumed.get(a["contract_id"], next(c for c in ref.contracts if c["contract_id"] == a["contract_id"])["consumed_value"]) + a["cost"]
        others = [f"{x['contract_id']} @ {P.inr(x['rate'])}" + ("" if x["has_room"] else " (ceiling exhausted)") for x in ca["applicable"] if x["contract_id"] != a["contract_id"]]
        reason = (f"{clause_txt}: rate contract {a['contract_id']} with {a['supplier_id']} is valid on {req.req_date}, supplier Active, "
                  f"ceiling room {P.inr(a['remaining'])} >= requirement {P.inr(a['cost'])} at {P.inr(a['rate'])}/unit; contract rate used regardless of value")
        if others:
            reason += f". Other applicable contracts: {'; '.join(others)} - lowest rate with room chosen (4.3)"
        res["consumed_after"] = round(consumed[a["contract_id"]], 2)
    elif mode == "preferred_first":
        top = pref[0]
        res.update(supplier_id=top["supplier_id"], unit_price=round(top["reference_price"], 2),
                   reference_po=top["reference_source"].get("po_number") or top["reference_source"].get("contract_id"))
        lastpo = bought[-1]
        reason = (f"{clause_txt}: " + "; ".join(why) + f"; item bought from an Active supplier in the last {pol['parameters']['history_lookback_days']} days "
                  f"(latest PO {lastpo['po_number']} on {lastpo['po_date']} from {lastpo['supplier_id']}). "
                  f"1st preferred supplier {top['supplier_id']} (score {top['score']}, {top['po_count']} PO(s) in {pol['parameters']['preferred_lookback_days']} days) "
                  f"at reference price {P.inr(top['reference_price'])} from {top['reference_source']['type']} {top['reference_source'].get('po_number') or top['reference_source'].get('contract_id')}")
    elif mode == "suggest":
        n = int((outcome["supplier"]).get("count", 3))
        if req.item_code:
            sg = suggest_suppliers(ref, req.item_code, n, req.req_date, pol)
            res.update(suggested_supplier_ids=sg["ids"], suggestions_detail=sg["suppliers"])
            src = "; ".join(f"{s['supplier_id']} ({s['source']})" for s in sg["suppliers"])
            reason = f"{clause_txt}: " + ("; ".join(why) + ". " if why else "") + f"Suggested {len(sg['ids'])} of {n}: {src}"
            if len(sg["ids"]) < n:
                reason += f" (only {len(sg['ids'])} eligible suppliers exist for the item/category)"
        else:
            reason = f"{clause_txt}: new item, no suggestions possible"
        if ca["applicable"] and not ca["usable"]:
            x = min(ca["applicable"], key=lambda a: a["rate"])
            reason += f". Contract {x['contract_id']} applies but ceiling is exhausted ({P.inr(x['remaining'])} left < {P.inr(x['cost'])} needed, 4.2); valued at lowest applicable contract rate"
    else:
        reason = f"{clause_txt}: " + ("; ".join(why) if why else "") + (". New item (not in item master): never clubbed, valued at requester's estimate, buyer finds suppliers" if req.item_code is None else "")
    if ca["notes"]:
        reason += ". Contracts not applicable: " + "; ".join(ca["notes"])
    if skipped_notes:
        reason += ". Note: " + "; ".join(skipped_notes)
    reason += f". Value {P.inr(value)} = {req.quantity:g} x {P.inr(unit_price, 2)} ({vsrc['text']}). Policy v{pol['version']}."
    res["reason"] = reason
    # 5.7 safety net: never output a non-eligible supplier
    for sid in ([res["supplier_id"]] if res["supplier_id"] else []) + res["suggested_supplier_ids"]:
        assert ref.supplier_ok(sid, pol), f"engine would use ineligible supplier {sid}"
    return res


# ------------------------------------------------------------------------------------------
# Consolidation (7.1 / 7.2)
# ------------------------------------------------------------------------------------------
def _consolidate(reqs: list[Requirement]) -> list[dict]:
    pos: list[dict] = []
    for r in sorted(reqs, key=lambda r: r.sort_key):
        res = r.result
        if res["decision"] != "PO":
            continue
        cons = r.policy["consolidation"]
        placed = None
        if cons.get("enabled", True):
            for po in pos:  # earliest open PO first
                if all(po[k] == res[k] for k in cons.get("same", [])) and \
                        (r.first_created - po["first_created"]).total_seconds() <= cons.get("window_hours", 24) * 3600:
                    placed = po
                    break
        if placed is None:
            placed = {"po_id": f"PO-{len(pos) + 1:04d}", "supplier_id": res["supplier_id"], "plant": res["plant"], "route": res["route"],
                      "first_created": r.first_created, "groups": [], "total_value": 0.0, "policy_version": res["policy_version"]}
            pos.append(placed)
        placed["groups"].append(res["group_id"])
        placed["total_value"] += res["quantity"] * res["unit_price"]
        res["po_id"] = placed["po_id"]
    for po in pos:
        po["first_created"] = po["first_created"].isoformat(sep=" ", timespec="minutes")
        po["total_value"] = round(po["total_value"], 2)
    return pos


# ------------------------------------------------------------------------------------------
# Public entry point
# ------------------------------------------------------------------------------------------
def run_engine(rows: list[dict], ref: Reference, policies: list[dict], matcher: ItemMatcher) -> dict:
    """rows: dicts in the PR batch CSV format.  policies: approved policy docs (normalised)."""
    policies = [P.normalise_policy(p) for p in policies]
    consumed: dict[str, float] = {}
    lines, skipped = lines_from_rows(rows, ref.plants)
    for l in lines:
        m = matcher.match(l.description, l.item_code)
        if m.status in ("matched", "exact_code"):
            l.resolved = m.item_code
        elif m.status == "ambiguous" and m.score >= 0.65 and len(m.candidates) > 1 and m.score - m.candidates[1].score >= 0.15:
            l.resolved, l.match_note = m.candidates[0].item_code, f"fuzzy match {m.score:.2f}"
        elif m.status == "ambiguous" and m.score >= 0.65 and len(m.candidates) == 1:
            l.resolved, l.match_note = m.candidates[0].item_code, f"fuzzy match {m.score:.2f}"
        else:
            l.resolved = None
            l.match_note = m.note or ("no item master item matches" if not m.candidates else f"closest '{m.candidates[0].description}' only {m.candidates[0].score:.2f}")
    reqs = _club(lines, policies)
    for r in sorted(reqs, key=lambda r: r.sort_key):  # contract ceiling is consumed in requirement-date order
        r.result = _decide(ref, r, consumed)
    pos = _consolidate(reqs)

    line_rows = []
    for r in reqs:
        res = r.result
        for l in r.lines:
            line_rows.append({
                "pr_no": l.pr_no, "pr_line": l.pr_line, "resolved_item_code": res["item_code"], "group_id": res["group_id"],
                "po_id": res["po_id"] or "", "decision": res["decision"], "route": res["route"],
                "supplier_id": res["supplier_id"] or "", "unit_price": res["unit_price"] if res["unit_price"] is not None else "",
                "group_value_inr": round(res["value"], 2), "suggested_supplier_ids": ";".join(res["suggested_supplier_ids"]),
                "policy_version": res["policy_version"],
                "reason": res["reason"] + (f" [item match: {l.match_note}]" if l.match_note and not res["item_code"] == "NEW" else (f" [{l.match_note}]" if l.match_note else "")),
                "description": l.description, "quantity": l.quantity, "plant": l.plant,
            })
    for s in skipped:
        line_rows.append({"pr_no": s["pr_no"], "pr_line": s["pr_line"], "resolved_item_code": s["resolved_item_code"], "group_id": "", "po_id": "",
                          "decision": "", "route": NOT_PROCESSED, "supplier_id": "", "unit_price": "", "group_value_inr": "",
                          "suggested_supplier_ids": "", "policy_version": "", "reason": "Not processed: " + s["reason"], "description": "", "quantity": "", "plant": ""})
    line_rows.sort(key=lambda x: (x["pr_no"], x["pr_line"]))
    return {"lines": line_rows, "requirements": [r.result for r in reqs], "pos": pos,
            "consumed_after": {k: round(v, 2) for k, v in consumed.items()},
            "policy_versions_used": sorted({r.result["policy_version"] for r in reqs})}


EXPORT_COLUMNS = ["pr_no", "pr_line", "resolved_item_code", "group_id", "po_id", "decision", "route", "supplier_id", "unit_price",
                  "group_value_inr", "suggested_supplier_ids", "policy_version", "reason"]
