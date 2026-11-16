"""Policy-as-data: schema, validation, condition evaluation, plain-language rendering and diffing.

A policy version is a JSON document (see policy/policy_v5_0.json).  Nothing in the decision engine
hard-codes a limit, window, rule order or route; it only knows how to evaluate this document.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
from typing import Any

# --------------------------------------------------------------------------------------
# Field catalogue: what a rule condition may talk about.  The engine fills these "facts" for each
# requirement.  Adding a new fact = adding one entry here + one line in engine.build_facts.
# --------------------------------------------------------------------------------------
FIELDS: dict[str, dict[str, str]] = {
    "value": {"type": "number", "label": "requirement value (INR, excl. GST)"},
    "quantity": {"type": "number", "label": "total quantity"},
    "unit_price": {"type": "number", "label": "valuation unit price"},
    "item_type": {"type": "string", "label": "item type (Material / Service / Capex)"},
    "category": {"type": "string", "label": "item category"},
    "item_code": {"type": "string", "label": "item code"},
    "plant": {"type": "string", "label": "plant (PUN1 / CHK1)"},
    "department": {"type": "list", "label": "requesting department(s)"},
    "urgency": {"type": "string", "label": "urgency (Emergency / Normal)"},
    "is_emergency": {"type": "bool", "label": "marked Emergency"},
    "item_is_new": {"type": "bool", "label": "item not in item master"},
    "contract_usable": {"type": "bool", "label": "a rate contract can be used (valid, Active supplier, ceiling left)"},
    "contract_applicable": {"type": "bool", "label": "a valid rate contract exists (ceiling may be exhausted)"},
    "bought_recently": {"type": "bool", "label": "item bought from an Active supplier in the history window"},
    "purchase_count": {"type": "number", "label": "POs for the item in the history window"},
    "preferred_supplier_count": {"type": "number", "label": "suppliers on the preferred list"},
    "line_count": {"type": "number", "label": "number of PR lines clubbed in the requirement"},
    "need_by_days": {"type": "number", "label": "days from requirement date to need-by date"},
}
OPERATORS = ["==", "!=", "<", "<=", ">", ">=", "in", "not_in", "contains"]
SUPPLIER_MODES = ["contract", "preferred_first", "suggest", "none"]
DECISIONS = ["PO", "SOURCING_REQUEST"]
KNOWN_ROUTES = ["CONTRACT_PO", "EMERGENCY_PO", "SPOT_PO", "RFQ", "RFP", "CAPEX_SOURCING", "NEW_ITEM_SOURCING"]

DEFAULT_PARAMETERS = {
    "club_window_days": 14,
    "club_need_by_days": 7,
    "price_lookback_days": 365,
    "history_lookback_days": 365,
    "preferred_lookback_days": 548,
}
DEFAULT_RANKING = {
    "weights": {"frequency": 0.30, "recency": 0.25, "contract": 0.15, "on_time": 0.15, "quality": 0.15},
    "frequency_cap": 5,
    "recency_full_days": 90,
    "recency_half_days": 365,
    "topup_weights": {"on_time": 0.5, "quality": 0.5},
}
DEFAULT_CONSOLIDATION = {"enabled": True, "window_hours": 24, "same": ["supplier_id", "plant", "route"]}


class PolicyError(ValueError):
    pass


# --------------------------------------------------------------------------------------
# Validation / normalisation
# --------------------------------------------------------------------------------------
def _validate_condition(c: Any, path: str, errors: list[str]) -> None:
    if not isinstance(c, dict):
        errors.append(f"{path}: condition must be an object")
        return
    if "all" in c or "any" in c:
        key = "all" if "all" in c else "any"
        if not isinstance(c[key], list) or not c[key]:
            errors.append(f"{path}.{key}: must be a non-empty list")
            return
        for i, sub in enumerate(c[key]):
            _validate_condition(sub, f"{path}.{key}[{i}]", errors)
    elif "not" in c:
        _validate_condition(c["not"], f"{path}.not", errors)
    elif "field" in c:
        if c["field"] not in FIELDS:
            errors.append(f"{path}: unknown field '{c['field']}' (known: {', '.join(FIELDS)})")
        if c.get("op") not in OPERATORS:
            errors.append(f"{path}: unknown operator '{c.get('op')}'")
        if "value" not in c:
            errors.append(f"{path}: missing value")
    else:
        errors.append(f"{path}: condition needs 'field', 'all', 'any' or 'not'")


def validate_policy(doc: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["policy must be a JSON object"]
    if not doc.get("version"):
        errors.append("missing 'version'")
    try:
        dt.date.fromisoformat(str(doc.get("effective_date")))
    except Exception:
        errors.append("'effective_date' must be YYYY-MM-DD")
    rules = doc.get("rules")
    if not isinstance(rules, list) or not rules:
        errors.append("'rules' must be a non-empty list")
        rules = []
    seen = set()
    for i, r in enumerate(rules):
        p = f"rules[{i}]"
        if not isinstance(r, dict):
            errors.append(f"{p}: must be an object")
            continue
        rid = r.get("id")
        if not rid:
            errors.append(f"{p}: missing id")
        elif rid in seen:
            errors.append(f"{p}: duplicate id {rid}")
        seen.add(rid)
        if "when" in r and r["when"] is not None:
            _validate_condition(r["when"], f"{p}.when", errors)
        out = r.get("outcome") or {}
        if out.get("decision") not in DECISIONS:
            errors.append(f"{p}.outcome.decision must be one of {DECISIONS}")
        if not out.get("route"):
            errors.append(f"{p}.outcome.route is required")
        sup = out.get("supplier") or {}
        if sup.get("mode") not in SUPPLIER_MODES:
            errors.append(f"{p}.outcome.supplier.mode must be one of {SUPPLIER_MODES}")
        if out.get("decision") == "PO" and sup.get("mode") not in ("contract", "preferred_first"):
            errors.append(f"{p}: a PO outcome needs supplier mode 'contract' or 'preferred_first'")
        if out.get("decision") == "SOURCING_REQUEST" and sup.get("mode") not in ("suggest", "none"):
            errors.append(f"{p}: a sourcing outcome needs supplier mode 'suggest' or 'none'")
    return errors


def normalise_policy(doc: dict) -> dict:
    """Fill defaults so the engine can rely on every section being present."""
    d = copy.deepcopy(doc)
    d["parameters"] = {**DEFAULT_PARAMETERS, **(d.get("parameters") or {})}
    rk = copy.deepcopy(DEFAULT_RANKING)
    user = d.get("supplier_ranking") or {}
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(rk.get(k), dict):
            rk[k].update(v)
        else:
            rk[k] = v
    d["supplier_ranking"] = rk
    d["consolidation"] = {**DEFAULT_CONSOLIDATION, **(d.get("consolidation") or {})}
    d.setdefault("eligible_supplier_statuses", ["Active"])
    d.setdefault("fallback_outcome", {"decision": "SOURCING_REQUEST", "route": "RFQ", "supplier": {"mode": "suggest", "count": 3}})
    for r in d.get("rules", []):
        r.setdefault("clause", r.get("id"))
        r.setdefault("name", r.get("id"))
    return d


# --------------------------------------------------------------------------------------
# Condition evaluation
# --------------------------------------------------------------------------------------
def _cmp(actual: Any, op: str, expected: Any) -> bool:
    if op == "contains":
        return expected in actual if isinstance(actual, (list, tuple, set, str)) else False
    if actual is None:
        return op in ("!=", "not_in")
    if op in ("in", "not_in"):
        exp = expected if isinstance(expected, (list, tuple, set)) else [expected]
        hit = _eq_any(actual, exp)
        return hit if op == "in" else not hit
    if isinstance(actual, (list, tuple, set)):
        hit = any(_eq(a, expected) for a in actual)
        return hit if op == "==" else (not hit if op == "!=" else False)
    if op == "==":
        return _eq(actual, expected)
    if op == "!=":
        return not _eq(actual, expected)
    try:
        a, e = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    return {"<": a < e, "<=": a <= e, ">": a > e, ">=": a >= e}[op]


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


def _eq_any(a: Any, options: list) -> bool:
    if isinstance(a, (list, tuple, set)):
        return any(_eq(x, o) for x in a for o in options)
    return any(_eq(a, o) for o in options)


def evaluate(cond: dict | None, facts: dict) -> bool:
    if not cond:
        return True
    if "all" in cond:
        return all(evaluate(c, facts) for c in cond["all"])
    if "any" in cond:
        return any(evaluate(c, facts) for c in cond["any"])
    if "not" in cond:
        return not evaluate(cond["not"], facts)
    return _cmp(facts.get(cond["field"]), cond["op"], cond["value"])


def condition_trace(cond: dict | None, facts: dict) -> list[str]:
    """Plain-language list of the facts that made a condition true (for the 'why' panel)."""
    out: list[str] = []

    def walk(c: dict):
        if "all" in c:
            for s in c["all"]:
                walk(s)
        elif "any" in c:
            for s in c["any"]:
                if evaluate(s, facts):
                    walk(s)
                    break
        elif "not" in c:
            out.append("not (" + describe_condition(c["not"]) + ")")
        else:
            f = c["field"]
            out.append(f"{describe_condition(c)} [actual: {_fmt_fact(f, facts.get(f))}]")

    if cond:
        walk(cond)
    return out


def _fmt_fact(field: str, v: Any) -> str:
    if field in ("value", "unit_price") and isinstance(v, (int, float)):
        return inr(v)
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, list):
        return ", ".join(map(str, v))
    return str(v)


# --------------------------------------------------------------------------------------
# Plain-language rendering
# --------------------------------------------------------------------------------------
def inr(v: float | int | None, decimals: int | None = None) -> str:
    """Indian digit grouping: 1,26,472."""
    if v is None:
        return "-"
    neg = v < 0
    v = abs(v)
    if decimals is None:
        decimals = 0 if float(v).is_integer() else 2
    s = f"{v:.{decimals}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        whole = head + "," + tail
    return ("-" if neg else "") + "₹" + whole + ("." + frac if frac else "")


_OPS_TEXT = {"==": "is", "!=": "is not", "<": "<", "<=": "≤", ">": ">", ">=": "≥", "in": "is one of", "not_in": "is not one of", "contains": "includes"}


def describe_condition(c: dict | None) -> str:
    if not c:
        return "always"
    if "all" in c:
        return " AND ".join(_paren(describe_condition(s), s) for s in c["all"])
    if "any" in c:
        return " OR ".join(_paren(describe_condition(s), s) for s in c["any"])
    if "not" in c:
        return "NOT (" + describe_condition(c["not"]) + ")"
    f, op, v = c["field"], c["op"], c["value"]
    label = FIELDS.get(f, {}).get("label", f)
    if FIELDS.get(f, {}).get("type") == "bool" and op in ("==", "!="):
        truth = bool(v) == (op == "==")
        return label if truth else f"NOT {label}"
    if f in ("value", "unit_price") and isinstance(v, (int, float)):
        vs = inr(v)
    elif isinstance(v, list):
        vs = ", ".join(map(str, v))
    else:
        vs = str(v)
    short = {"value": "value", "item_type": "item type", "plant": "plant", "category": "category", "urgency": "urgency",
             "quantity": "quantity", "item_code": "item", "department": "department"}.get(f, label)
    return f"{short} {_OPS_TEXT[op]} {vs}"


def _paren(text: str, c: dict) -> str:
    return f"({text})" if ("any" in c or ("all" in c and len(c["all"]) > 1 and False)) else text


def describe_outcome(o: dict) -> str:
    sup = o.get("supplier") or {}
    mode = sup.get("mode")
    who = {
        "contract": "the contract supplier at the contract rate",
        "preferred_first": "the 1st preferred supplier at its reference price",
        "none": "no supplier suggestions (buyer finds suppliers)",
    }.get(mode, f"suggest {sup.get('count', 3)} suppliers")
    kind = "Place a PO" if o.get("decision") == "PO" else "Raise a sourcing request"
    extra = f" — {o['notes']}" if o.get("notes") else ""
    return f"{kind} ({o.get('route')}) with {who}{extra}"


def describe_rule(r: dict) -> str:
    return f"[{r.get('clause', r['id'])}] {r.get('name', '')}: when {describe_condition(r.get('when'))} → {describe_outcome(r['outcome'])}"


# --------------------------------------------------------------------------------------
# Diffing two policy documents into plain language
# --------------------------------------------------------------------------------------
PARAM_LABELS = {
    "club_window_days": "Clubbing window (days between a requirement's first line and a joining line)",
    "club_need_by_days": "Clubbing need-by tolerance (± days)",
    "price_lookback_days": "Valuation price lookback (days of PO history)",
    "history_lookback_days": "'Bought recently' lookback (days)",
    "preferred_lookback_days": "Preferred-supplier PO lookback (days)",
}


def diff_policies(old: dict, new: dict) -> list[dict]:
    """Return a list of {kind, text} changes, in plain language."""
    old, new = normalise_policy(old), normalise_policy(new)
    changes: list[dict] = []

    def add(kind: str, text: str):
        changes.append({"kind": kind, "text": text})

    if old.get("version") != new.get("version"):
        add("meta", f"Version {old.get('version')} → {new.get('version')}")
    if old.get("effective_date") != new.get("effective_date"):
        add("meta", f"Effective date {old.get('effective_date')} → {new.get('effective_date')}")
    for k in sorted(set(old["parameters"]) | set(new["parameters"])):
        a, b = old["parameters"].get(k), new["parameters"].get(k)
        if a != b:
            add("parameter", f"{PARAM_LABELS.get(k, k)}: {a} → {b}")

    orules = {r["id"]: r for r in old["rules"]}
    nrules = {r["id"]: r for r in new["rules"]}
    for rid, r in nrules.items():
        if rid not in orules:
            add("rule_added", "NEW RULE " + describe_rule(r))
    for rid, r in orules.items():
        if rid not in nrules:
            add("rule_removed", "REMOVED RULE " + describe_rule(r))
    for rid in nrules:
        if rid in orules:
            o, n = orules[rid], nrules[rid]
            if json.dumps(o.get("when"), sort_keys=True) != json.dumps(n.get("when"), sort_keys=True):
                add("rule_changed", f"Rule {n.get('clause', rid)} ({n.get('name', '')}) condition: when {describe_condition(o.get('when'))} → when {describe_condition(n.get('when'))}")
            if json.dumps(o["outcome"], sort_keys=True) != json.dumps(n["outcome"], sort_keys=True):
                add("rule_changed", f"Rule {n.get('clause', rid)} ({n.get('name', '')}) outcome: {describe_outcome(o['outcome'])} → {describe_outcome(n['outcome'])}")
    oorder = [r["id"] for r in old["rules"] if r["id"] in nrules]
    norder = [r["id"] for r in new["rules"] if r["id"] in orules]
    if oorder != norder:
        add("rule_order", "Rule order changed: " + " → ".join(r["id"] for r in new["rules"]))
    elif [r["id"] for r in new["rules"]] != [r["id"] for r in old["rules"]] and any(k["kind"] == "rule_added" for k in changes):
        pos_lines = []
        for i, r in enumerate(new["rules"]):
            if r["id"] not in orules:
                pos_lines.append(f"rule {r['id']} is evaluated at position {i + 1} of {len(new['rules'])}")
        if pos_lines:
            add("rule_order", "; ".join(pos_lines) + " (first matching rule wins)")
    if old["supplier_ranking"] != new["supplier_ranking"]:
        ow, nw = old["supplier_ranking"], new["supplier_ranking"]
        for k, v in nw.items():
            if ow.get(k) != v:
                add("ranking", f"Supplier ranking '{k}': {json.dumps(ow.get(k))} → {json.dumps(v)}")
    if old["consolidation"] != new["consolidation"]:
        add("consolidation", f"PO consolidation: {json.dumps(old['consolidation'])} → {json.dumps(new['consolidation'])}")
    if old["eligible_supplier_statuses"] != new["eligible_supplier_statuses"]:
        add("suppliers", f"Eligible supplier statuses: {old['eligible_supplier_statuses']} → {new['eligible_supplier_statuses']}")
    if json.dumps(old["fallback_outcome"], sort_keys=True) != json.dumps(new["fallback_outcome"], sort_keys=True):
        add("fallback", f"Fallback outcome: {describe_outcome(old['fallback_outcome'])} → {describe_outcome(new['fallback_outcome'])}")
    return changes


# --------------------------------------------------------------------------------------
# Selecting the policy version in force for a date
# --------------------------------------------------------------------------------------
def version_for_date(versions: list[dict], on: dt.date) -> dict | None:
    """versions: approved policy docs.  The latest effective_date <= on wins (ties: later-created / higher list index)."""
    best = None
    for v in versions:
        eff = dt.date.fromisoformat(v["effective_date"])
        if eff <= on and (best is None or eff >= dt.date.fromisoformat(best["effective_date"])):
            best = v
    if best is None and versions:
        best = min(versions, key=lambda v: v["effective_date"])  # before the first policy: use the earliest one
    return best


def next_version(existing: list[str]) -> str:
    best = (0, 0)
    for v in existing:
        m = re.match(r"^(\d+)\.(\d+)", str(v))
        if m:
            best = max(best, (int(m.group(1)), int(m.group(2))))
    return f"{best[0]}.{best[1] + 1}"
