"""Engine tests.  Synthetic fixtures always run; the worked-example tests run when KIT_DIR points at the task kit
(the kit is not stored in this repo)."""
import csv, datetime as dt, json, os, sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import policy as P
from app.engine import run_engine
from app.matching import ItemMatcher
from app.suppliers import Reference

POL = json.loads((Path(__file__).resolve().parents[2] / "policy" / "policy_v5_0.json").read_text())
D = dt.date.fromisoformat


def mini_ref(contracts=None, pos=None, sup_status="Active"):
    items = {"ITM-1": {"item_code": "ITM-1", "description": "Widget alpha 10", "uom": "NOS", "category": "Cat", "item_type": "Material", "standard_price": 100.0},
             "ITM-2": {"item_code": "ITM-2", "description": "Big crane 5 ton", "uom": "NOS", "category": "Cap", "item_type": "Capex", "standard_price": 1e6}}
    sup = {s: {"supplier_id": s, "name": s, "categories": "Cat;Cap", "status": st, "on_time_pct": 90, "quality_rating": 4}
           for s, st in (("S1", sup_status), ("S2", "Active"), ("S3", "Blocked"))}
    pos = pos if pos is not None else [{"po_number": "1", "po_date": D("2026-10-01"), "supplier_id": "S1", "item_code": "ITM-1", "quantity": 1, "unit_price": 100.0, "plant": "PUN1", "delivered_on_time": True}]
    return Reference(items, sup, {"PUN1": "Pune", "CHK1": "Chakan"}, pos, contracts or [])


def row(pr, line, ts, plant="PUN1", code="ITM-1", qty=10, need="2026-11-30", urg="Normal", desc="Widget alpha 10", price=""):
    return {"pr_no": pr, "pr_line": line, "created_at": ts, "plant": plant, "item_code": code, "description": desc, "quantity": qty, "uom": "NOS",
            "estimated_unit_price": price, "need_by_date": need, "urgency": urg, "requester": "x", "department": "d"}


def run(rows, ref, pol=POL):
    return run_engine(rows, ref, [pol], ItemMatcher(ref.items))


def test_spot_po_and_clubbing_prevents_split():
    ref = mini_ref()
    out = run([row("A", 1, "2026-11-02 09:00", qty=1500), row("B", 1, "2026-11-03 09:00", qty=1000)], ref)
    assert len({l["group_id"] for l in out["lines"]}) == 1          # clubbed
    assert out["lines"][0]["group_value_inr"] == 250000                 # 2500 x 100 -> over spot limit
    assert out["lines"][0]["route"] == "RFQ"


def test_blocked_supplier_never_used():
    ref = mini_ref(sup_status="Blocked")
    out = run([row("A", 1, "2026-11-02 09:00", qty=10)], ref)
    assert out["lines"][0]["route"] == "RFQ" and "S1" not in out["lines"][0]["suggested_supplier_ids"]


def test_zero_qty_and_unknown_plant_not_processed():
    out = run([row("A", 1, "2026-11-02 09:00", qty=0), row("B", 1, "2026-11-02 09:00", plant="PUN2")], mini_ref())
    assert [l["route"] for l in out["lines"]] == ["NOT_PROCESSED", "NOT_PROCESSED"]


def test_new_item_goes_to_sourcing():
    out = run([row("A", 1, "2026-11-02 09:00", code="", desc="Quantum flux capacitor", price="5000")], mini_ref())
    assert out["lines"][0]["resolved_item_code"] == "NEW" and out["lines"][0]["route"] == "NEW_ITEM_SOURCING"


def test_capex_beats_contract():
    c = [{"contract_id": "RC-1", "supplier_id": "S2", "start_date": D("2026-01-01"), "end_date": D("2026-12-31"), "value_cap": 1e9, "consumed_value": 0, "lines": {"ITM-2": 900000.0}}]
    out = run([row("A", 1, "2026-11-02 09:00", code="ITM-2", desc="Big crane 5 ton", qty=1)], mini_ref(contracts=c))
    assert out["lines"][0]["route"] == "CAPEX_SOURCING"


def test_contract_ceiling_consumed_in_order():
    c = [{"contract_id": "RC-1", "supplier_id": "S2", "start_date": D("2026-01-01"), "end_date": D("2026-12-31"), "value_cap": 1500.0, "consumed_value": 0, "lines": {"ITM-1": 90.0}}]
    out = run([row("A", 1, "2026-11-02 09:00", qty=10), row("B", 1, "2026-11-20 09:00", qty=10)], mini_ref(contracts=c))
    assert [l["route"] for l in out["lines"]] == ["CONTRACT_PO", "SPOT_PO"]      # 900 fits, second 900 > 600 left


def test_policy_amendment_changes_behaviour_without_code():
    ref = mini_ref()
    rows = [row("A", 1, "2026-11-02 09:00", qty=1500)]            # value 150,000
    assert run(rows, ref)["lines"][0]["route"] == "SPOT_PO"
    new = json.loads(json.dumps(POL)); new["version"] = "5.1"
    for r in new["rules"]:
        if r["id"] == "5.4":
            r["when"]["all"][0]["value"] = 100000
    assert run(rows, ref, new)["lines"][0]["route"] == "RFQ"


def test_inr_format():
    assert P.inr(126471.6) == "₹1,26,471.60" and P.inr(200000) == "₹2,00,000"


KIT = os.environ.get("KIT_DIR")


@pytest.mark.skipif(not KIT, reason="set KIT_DIR to the task kit to run the worked-example checks")
def test_worked_examples():
    from app import loaders
    kit = loaders.collect_kit(loaders.files_from_dir(KIT))
    items, sup, plants, pos, contracts = loaders.build_reference_dicts(kit)
    ref = Reference(items, sup, plants, pos, contracts)
    rows = list(csv.DictReader(open(Path(KIT) / "pr_batches" / "sample_pr_batch.csv")))
    out = run_engine(rows, ref, [POL], ItemMatcher(items))
    by = {(l["pr_no"], l["pr_line"]): l for l in out["lines"]}
    exp = {("PR-26-00019", 1): ("CONTRACT_PO", "S001", 137530.65), ("PR-26-00004", 1): ("RFQ", "", 220255.6), ("PR-26-00034", 1): ("CONTRACT_PO", "S007", 32546.6),
           ("PR-26-00146", 1): ("SPOT_PO", "S024", 126471.6), ("PR-26-00106", 1): ("SPOT_PO", "S027", 156170.3), ("PR-26-00059", 1): ("EMERGENCY_PO", "S014", 253184.4),
           ("PR-26-00100", 1): ("RFP", "", 2070780.0), ("PR-26-00047", 1): ("CAPEX_SOURCING", "", 6036704.08), ("PR-26-00170", 1): ("NEW_ITEM_SOURCING", "", 141100.0),
           ("PR-26-00172", 1): ("NOT_PROCESSED", "", ""), ("PR-26-00173", 1): ("NOT_PROCESSED", "", "")}
    for k, (route, sup_id, val) in exp.items():
        assert by[k]["route"] == route and by[k]["supplier_id"] == sup_id, k
        if val != "":
            assert abs(by[k]["group_value_inr"] - val) < 0.5, k
