"""Preferred supplier lists (policy section 6): membership, ranking, reference price, sourcing suggestions."""
from __future__ import annotations

import bisect
import datetime as dt
from dataclasses import dataclass, field


@dataclass
class Reference:
    """Immutable-ish snapshot of master data the engine works on."""
    items: dict[str, dict]
    suppliers: dict[str, dict]
    plants: dict[str, str]
    po_history: list[dict]            # each: po_number, po_date(date), supplier_id, item_code, quantity, unit_price, plant, delivered_on_time
    contracts: list[dict]             # each: contract_id, supplier_id, start_date(date), end_date(date), value_cap, consumed_value, lines{item_code: rate}
    _po_by_item: dict[str, list[dict]] = field(default_factory=dict, repr=False)
    _po_dates: dict[str, list[dt.date]] = field(default_factory=dict, repr=False)
    _cache: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        by: dict[str, list[dict]] = {}
        for p in self.po_history:
            by.setdefault(p["item_code"], []).append(p)
        for code, lst in by.items():
            lst.sort(key=lambda p: (p["po_date"], int(p["po_number"]) if str(p["po_number"]).isdigit() else 0))
        self._po_by_item = by
        self._po_dates = {c: [p["po_date"] for p in lst] for c, lst in by.items()}

    # ---- PO history helpers -------------------------------------------------------------
    def pos_in_window(self, item_code: str, as_of: dt.date, days: int) -> list[dict]:
        """POs for the item with as_of - days <= po_date <= as_of."""
        lst = self._po_by_item.get(item_code, [])
        dates = self._po_dates.get(item_code, [])
        lo = bisect.bisect_left(dates, as_of - dt.timedelta(days=days))
        hi = bisect.bisect_right(dates, as_of)
        return lst[lo:hi]

    def supplier_ok(self, sid: str, policy: dict) -> bool:
        s = self.suppliers.get(sid)
        return bool(s) and s["status"] in policy["eligible_supplier_statuses"]

    def contracts_for_item(self, item_code: str) -> list[dict]:
        return [c for c in self.contracts if item_code in c["lines"]]


def _score_supplier(f: dict, rk: dict) -> tuple[float, dict]:
    w = rk["weights"]
    freq = min(f["po_count"], rk["frequency_cap"]) / rk["frequency_cap"] if rk["frequency_cap"] else 0.0
    parts = {
        "frequency": freq,
        "recency": f["recency"],
        "contract": 1.0 if f["had_contract"] else 0.0,
        "on_time": f["on_time_pct"] / 100.0,
        "quality": f["quality"] / 5.0,
    }
    total = sum(w.get(k, 0.0) * v for k, v in parts.items())
    return total, {k: {"value": round(v, 3), "weight": w.get(k, 0.0), "points": round(w.get(k, 0.0) * v, 4)} for k, v in parts.items()}


def policy_key(policy: dict) -> str:
    import json
    return json.dumps([policy.get("version"), policy["parameters"]["preferred_lookback_days"], policy["supplier_ranking"], policy["eligible_supplier_statuses"]], sort_keys=True)


def preferred_list(ref: Reference, item_code: str, as_of: dt.date, policy: dict) -> list[dict]:
    """Ranked preferred suppliers for an item as of a date (policy 6.1-6.3)."""
    key = (item_code, as_of, policy_key(policy))
    if key in ref._cache:
        return ref._cache[key]
    rk = policy["supplier_ranking"]
    look = policy["parameters"]["preferred_lookback_days"]
    pos = ref.pos_in_window(item_code, as_of, look)
    by_sup: dict[str, list[dict]] = {}
    for p in pos:
        by_sup.setdefault(p["supplier_id"], []).append(p)
    contracts_by_sup: dict[str, list[dict]] = {}
    for c in ref.contracts_for_item(item_code):
        if c["start_date"] <= as_of:
            contracts_by_sup.setdefault(c["supplier_id"], []).append(c)
    out = []
    for sid in sorted(set(by_sup) | set(contracts_by_sup)):
        if not ref.supplier_ok(sid, policy):
            continue  # 5.7 / 6.1: Active suppliers only
        s = ref.suppliers[sid]
        sp = by_sup.get(sid, [])
        last = sp[-1] if sp else None
        if last:
            age = (as_of - last["po_date"]).days
            recency = 1.0 if age <= rk["recency_full_days"] else (0.5 if age <= rk["recency_half_days"] else 0.0)
        else:
            recency = 0.0
        feats = {
            "po_count": len(sp), "recency": recency, "had_contract": sid in contracts_by_sup,
            "on_time_pct": float(s["on_time_pct"]), "quality": float(s["quality_rating"]),
        }
        score, parts = _score_supplier(feats, rk)
        # 6.3 reference price: most recent PO price in window, else rate in most recent contract
        if last:
            ref_price, ref_src = last["unit_price"], {"type": "PO", "po_number": last["po_number"], "date": last["po_date"].isoformat()}
        else:
            c = max(contracts_by_sup[sid], key=lambda c: c["start_date"])
            ref_price, ref_src = c["lines"][item_code], {"type": "CONTRACT", "contract_id": c["contract_id"], "date": c["start_date"].isoformat()}
        out.append({
            "supplier_id": sid, "supplier_name": s["name"], "score": round(score, 4), "breakdown": parts,
            "po_count": len(sp), "last_po_date": last["po_date"].isoformat() if last else None,
            "recency": recency, "had_contract": feats["had_contract"],
            "contract_ids": sorted(c["contract_id"] for c in contracts_by_sup.get(sid, [])),
            "on_time_pct": feats["on_time_pct"], "quality": feats["quality"],
            "reference_price": round(ref_price, 2), "reference_source": ref_src,
        })
    # best first; ties broken by more recent PO, then supplier id, so the ranking is deterministic
    out.sort(key=lambda r: (-r["score"], -(dt.date.fromisoformat(r["last_po_date"]).toordinal() if r["last_po_date"] else 0), r["supplier_id"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    ref._cache[key] = out
    return out


def suggest_suppliers(ref: Reference, item_code: str, n: int, as_of: dt.date, policy: dict) -> dict:
    """Policy 6.4: preferred list in order, topped up with Active category suppliers (best delivery + quality)."""
    pref = preferred_list(ref, item_code, as_of, policy)
    chosen = [{"supplier_id": p["supplier_id"], "source": "preferred", "rank": p["rank"]} for p in pref[:n]]
    if len(chosen) < n:
        cat = ref.items[item_code]["category"]
        tw = policy["supplier_ranking"]["topup_weights"]
        have = {c["supplier_id"] for c in chosen}
        pool = []
        for sid, s in ref.suppliers.items():
            if sid in have or not ref.supplier_ok(sid, policy):
                continue
            cats = [c.strip().lower() for c in str(s["categories"]).split(";")]
            if cat.lower() not in cats:
                continue
            sc = tw.get("on_time", 0.5) * (float(s["on_time_pct"]) / 100.0) + tw.get("quality", 0.5) * (float(s["quality_rating"]) / 5.0)
            pool.append((-sc, sid))
        pool.sort()
        for sc, sid in pool[: n - len(chosen)]:
            chosen.append({"supplier_id": sid, "source": f"category top-up ({cat})", "score": round(-sc, 3)})
    return {"suppliers": chosen, "ids": [c["supplier_id"] for c in chosen], "preferred_count": len(pref)}


def export_preferred_rows(ref: Reference, as_of: dt.date, policy: dict, top: int = 3) -> list[dict]:
    rows = []
    for code in sorted(ref.items):
        for p in preferred_list(ref, code, as_of, policy)[:top]:
            rows.append({"item_code": code, "rank": p["rank"], "supplier_id": p["supplier_id"], "reference_price": p["reference_price"]})
    return rows
