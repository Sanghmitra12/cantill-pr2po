"""Application services: master data, policies (versioned), batch runs, buyer actions, audit trail."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import threading
from pathlib import Path

from . import db, loaders, policy as P
from .contract_pdf import parse_contract_pdf
from .engine import EXPORT_COLUMNS, run_engine
from .matching import ItemMatcher
from .suppliers import Reference, export_preferred_rows, preferred_list

POLICY_SEED = Path(os.environ.get("POLICY_SEED", Path(__file__).resolve().parents[2] / "policy" / "policy_v5_0.json"))
LIVE_BATCH_NAME = "Live chat queue"
_cache_lock = threading.RLock()
_cache: dict = {"ver": None, "ref": None, "matcher": None}


def now() -> str:
    return dt.datetime.now().isoformat(sep=" ", timespec="seconds")


# ---------------------------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------------------------
def audit(user: dict, action: str, entity: str, entity_id: str | int | None, detail: dict | None = None) -> None:
    db.ex("INSERT INTO audit_log(ts,user,role,action,entity,entity_id,detail) VALUES(?,?,?,?,?,?,?)",
          (now(), user.get("name", "system"), user.get("role", ""), action, entity, str(entity_id or ""), db.jdump(detail or {})))


# ---------------------------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------------------------
def init_app() -> None:
    db.init()
    if not db.q1("SELECT 1 FROM policies LIMIT 1"):
        doc = json.loads(POLICY_SEED.read_text())
        db.ex("INSERT INTO policies(version,effective_date,status,doc,source_kind,source_name,changes,notes,created_by,created_at,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (doc["version"], doc["effective_date"], "approved", db.jdump(doc), "seed", POLICY_SEED.name, "[]", "Initial policy encoded from CORVO-PROC-POL-04 v5.0", "system", now(), "system", now()))
    if not db.q1("SELECT 1 FROM batches WHERE kind='live'"):
        db.ex("INSERT INTO batches(name,kind,created_by,created_at) VALUES(?,?,?,?)", (LIVE_BATCH_NAME, "live", "system", now()))
    seed = os.environ.get("SEED_DATA_DIR") or os.environ.get("SEED_DATA_ZIP")
    if seed and not db.q1("SELECT 1 FROM items LIMIT 1"):
        try:
            files = loaders.files_from_zip(Path(seed).read_bytes()) if seed.endswith(".zip") else loaders.files_from_dir(seed)
            load_master(files, {"name": "system", "role": "admin"})
        except Exception as e:  # noqa: BLE001
            print("seed failed:", e)
    url = os.environ.get("SEED_DATA_URL")
    if url and not db.q1("SELECT 1 FROM items LIMIT 1"):
        try:
            import httpx
            r = httpx.get(url, follow_redirects=True, timeout=60)
            r.raise_for_status()
            load_master(loaders.files_from_zip(r.content), {"name": "system", "role": "admin"})
        except Exception as e:  # noqa: BLE001
            print("seed from URL failed:", e)


def live_batch_id() -> int:
    return db.q1("SELECT id FROM batches WHERE kind='live'")["id"]


# ---------------------------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------------------------
def load_master(files: dict[str, bytes], user: dict) -> dict:
    kit = loaders.collect_kit(files)
    counts = {}
    with db.conn() as c:
        if kit["items"]:
            c.execute("DELETE FROM items")
            c.executemany("INSERT INTO items VALUES(?,?,?,?,?,?)", [(r["item_code"], r["description"], r["uom"], r["category"], r["item_type"], float(r["standard_price"])) for r in kit["items"]])
            counts["items"] = len(kit["items"])
        if kit["suppliers"]:
            c.execute("DELETE FROM suppliers")
            c.executemany("INSERT INTO suppliers VALUES(?,?,?,?,?,?,?)", [(r["supplier_id"], r["name"], r.get("city", ""), r.get("categories", ""), r["status"], float(r["on_time_pct"]), float(r["quality_rating"])) for r in kit["suppliers"]])
            counts["suppliers"] = len(kit["suppliers"])
        if kit["plants"]:
            c.execute("DELETE FROM plants")
            c.executemany("INSERT INTO plants VALUES(?,?)", [(r["plant"], r["name"]) for r in kit["plants"]])
            counts["plants"] = len(kit["plants"])
        if kit["po_history"]:
            c.execute("DELETE FROM po_history")
            c.executemany("INSERT INTO po_history VALUES(?,?,?,?,?,?,?,?,?)", [
                (r["po_number"], r["po_date"], r["supplier_id"], r["item_code"], float(r["quantity"]), r.get("uom", ""), float(r["unit_price"]), r.get("plant", ""),
                 1 if str(r.get("delivered_on_time", "")).lower() in ("true", "1", "yes") else 0) for r in kit["po_history"]])
            counts["po_history"] = len(kit["po_history"])
        if kit["contract_register"]:
            c.execute("DELETE FROM contracts WHERE source IN ('register','pdf')")
            c.execute("DELETE FROM contract_lines WHERE contract_id NOT IN (SELECT contract_id FROM contracts)")
            c.executemany("INSERT OR REPLACE INTO contracts VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
                (r["contract_id"], r["supplier_id"], r.get("supplier_name", ""), r["start_date"], r["end_date"], float(r["value_cap"]), float(r["consumed_value"]), "register", None, user["name"], now()) for r in kit["contract_register"]])
            counts["contracts"] = len(kit["contract_register"])
        if kit["contract_lines"]:
            c.executemany("INSERT OR REPLACE INTO contract_lines VALUES(?,?,?,?,?)", [(r["contract_id"], r["item_code"], r.get("description", ""), r.get("uom", ""), float(r["rate"])) for r in kit["contract_lines"]])
            counts["contract_lines"] = len(kit["contract_lines"])
    # PDF-only contracts (not in the register): extract and add
    added_pdf = []
    if kit["pdfs"]:
        items = {r["item_code"]: r for r in db.q("SELECT * FROM items")}
        known = {r["contract_id"] for r in db.q("SELECT contract_id FROM contracts")}
        for name, data in kit["pdfs"].items():
            p = parse_contract_pdf(data, items)
            if p.get("contract_id") and p["contract_id"] not in known and not p["warnings"] and p["lines"]:
                save_contract(p, user, source="pdf", pdf_name=name)
                added_pdf.append(p["contract_id"])
    counts["pdf_only_contracts_added"] = added_pdf
    bump_data()
    audit(user, "load_master_data", "master", None, counts)
    return counts


def save_contract(c: dict, user: dict, source: str = "manual", pdf_name: str | None = None) -> dict:
    errs = []
    for k in ("contract_id", "supplier_id", "start_date", "end_date", "value_cap"):
        if not c.get(k):
            errs.append(f"{k} is required")
    if not c.get("lines"):
        errs.append("at least one item line is required")
    if errs:
        raise ValueError("; ".join(errs))
    if not db.q1("SELECT 1 FROM suppliers WHERE supplier_id=?", (c["supplier_id"],)):
        raise ValueError(f"unknown supplier {c['supplier_id']}")
    dt.date.fromisoformat(c["start_date"])
    dt.date.fromisoformat(c["end_date"])
    if db.q1("SELECT 1 FROM contracts WHERE contract_id=?", (c["contract_id"],)):
        raise ValueError(f"contract {c['contract_id']} already exists")
    items = {r["item_code"] for r in db.q("SELECT item_code FROM items")}
    for l in c["lines"]:
        if l["item_code"] not in items:
            raise ValueError(f"unknown item {l['item_code']}")
    sname = c.get("supplier_name") or db.q1("SELECT name FROM suppliers WHERE supplier_id=?", (c["supplier_id"],))["name"]
    with db.conn() as cx:
        cx.execute("INSERT INTO contracts VALUES(?,?,?,?,?,?,?,?,?,?,?)", (c["contract_id"], c["supplier_id"], sname, c["start_date"], c["end_date"], float(c["value_cap"]),
                                                                           float(c.get("consumed_value") or 0), source, pdf_name, user["name"], now()))
        cx.executemany("INSERT INTO contract_lines VALUES(?,?,?,?,?)", [(c["contract_id"], l["item_code"], l.get("description", ""), l.get("uom", ""), float(l["rate"])) for l in c["lines"]])
    bump_data()
    audit(user, "add_contract", "contract", c["contract_id"], {"source": source, "pdf": pdf_name, "value_cap": c["value_cap"], "lines": len(c["lines"])})
    return {"contract_id": c["contract_id"]}


def data_version() -> str:
    return db.meta_get("data_version", "0") or "0"


def bump_data() -> None:
    db.meta_set("data_version", str(int(data_version()) + 1))


def get_reference() -> tuple[Reference, ItemMatcher]:
    with _cache_lock:
        ver = data_version()
        if _cache["ver"] == ver and _cache["ref"] is not None:
            return _cache["ref"], _cache["matcher"]
        kit = {
            "items": db.q("SELECT * FROM items"), "suppliers": db.q("SELECT * FROM suppliers"), "plants": db.q("SELECT * FROM plants"),
            "po_history": db.q("SELECT * FROM po_history"), "contract_register": [], "contract_lines": [], "pdfs": {},
        }
        for r in kit["po_history"]:
            r["delivered_on_time"] = bool(r["delivered_on_time"])
        items = {r["item_code"]: r for r in kit["items"]}
        suppliers = {r["supplier_id"]: r for r in kit["suppliers"]}
        plants = {r["plant"]: r["name"] for r in kit["plants"]}
        pos = [{**r, "po_date": dt.date.fromisoformat(r["po_date"])} for r in kit["po_history"]]
        lines: dict[str, dict] = {}
        for r in db.q("SELECT * FROM contract_lines"):
            lines.setdefault(r["contract_id"], {})[r["item_code"]] = r["rate"]
        contracts = [{**r, "start_date": dt.date.fromisoformat(r["start_date"]), "end_date": dt.date.fromisoformat(r["end_date"]),
                      "lines": lines.get(r["contract_id"], {})} for r in db.q("SELECT * FROM contracts")]
        ref = Reference(items, suppliers, plants, pos, contracts)
        _cache.update(ver=ver, ref=ref, matcher=ItemMatcher(items))
        return ref, _cache["matcher"]


def master_status() -> dict:
    return {t: db.q1(f"SELECT COUNT(*) n FROM {t}")["n"] for t in ("items", "suppliers", "po_history", "contracts", "plants")}


# ---------------------------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------------------------
def approved_docs() -> list[dict]:
    rows = db.q("SELECT * FROM policies WHERE status='approved' ORDER BY effective_date, id")
    return [P.normalise_policy(json.loads(r["doc"])) for r in rows]


def policy_rows() -> list[dict]:
    out = []
    today = dt.date.today().isoformat()
    approved = [r for r in db.q("SELECT * FROM policies WHERE status='approved' ORDER BY effective_date, id")]
    in_force = None
    for r in approved:
        if r["effective_date"] <= today:
            in_force = r["id"]
    for r in db.q("SELECT * FROM policies ORDER BY id DESC"):
        r["doc"] = json.loads(r["doc"])
        r["changes"] = json.loads(r["changes"] or "[]")
        r["in_force_today"] = r["id"] == in_force
        out.append(r)
    return out


def create_draft(doc: dict, user: dict, source_kind: str, source_name: str, source_text: str, changes: list[dict] | None = None, notes: str = "") -> dict:
    doc = P.normalise_policy(doc)
    errs = P.validate_policy(doc)
    if errs:
        raise P.PolicyError("; ".join(errs))
    base = P.version_for_date(approved_docs(), dt.date.fromisoformat(doc["effective_date"])) or {}
    if changes is None:
        changes = P.diff_policies(base, doc) if base else []
    pid = db.ex("INSERT INTO policies(version,effective_date,status,doc,source_kind,source_name,source_text,changes,notes,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (doc["version"], doc["effective_date"], "draft", db.jdump(doc), source_kind, source_name, source_text[:20000], db.jdump(changes), notes, user["name"], now()))
    audit(user, "policy_draft_created", "policy", pid, {"version": doc["version"], "effective_date": doc["effective_date"], "source": source_kind, "changes": len(changes)})
    return {"id": pid, "version": doc["version"], "changes": changes}


def update_draft(pid: int, doc: dict, user: dict) -> dict:
    row = db.q1("SELECT * FROM policies WHERE id=?", (pid,))
    if not row or row["status"] != "draft":
        raise ValueError("only drafts can be edited")
    doc = P.normalise_policy(doc)
    errs = P.validate_policy(doc)
    if errs:
        raise P.PolicyError("; ".join(errs))
    base = P.version_for_date([d for d in approved_docs()], dt.date.fromisoformat(doc["effective_date"])) or {}
    changes = P.diff_policies(base, doc) if base else []
    db.ex("UPDATE policies SET version=?,effective_date=?,doc=?,changes=? WHERE id=?", (doc["version"], doc["effective_date"], db.jdump(doc), db.jdump(changes), pid))
    audit(user, "policy_draft_edited", "policy", pid, {"changes": len(changes)})
    return {"id": pid, "changes": changes}


def decide_policy(pid: int, approve: bool, user: dict, note: str = "") -> dict:
    row = db.q1("SELECT * FROM policies WHERE id=?", (pid,))
    if not row or row["status"] != "draft":
        raise ValueError("policy is not a draft")
    if approve:
        doc = json.loads(row["doc"])
        clash = db.q1("SELECT id FROM policies WHERE status='approved' AND version=?", (doc["version"],))
        if clash:
            newv = P.next_version([r["version"] for r in db.q("SELECT version FROM policies")])
            doc["version"] = newv
            db.ex("UPDATE policies SET version=?, doc=? WHERE id=?", (newv, db.jdump(doc), pid))
        db.ex("UPDATE policies SET status='approved', decided_by=?, decided_at=?, notes=COALESCE(notes,'')||? WHERE id=?", (user["name"], now(), (" | " + note) if note else "", pid))
    else:
        db.ex("UPDATE policies SET status='rejected', decided_by=?, decided_at=?, notes=COALESCE(notes,'')||? WHERE id=?", (user["name"], now(), (" | " + note) if note else "", pid))
    audit(user, "policy_approved" if approve else "policy_rejected", "policy", pid, {"note": note})
    return {"id": pid, "status": "approved" if approve else "rejected"}


def latest_draft() -> dict | None:
    r = db.q1("SELECT * FROM policies WHERE status='draft' ORDER BY id DESC LIMIT 1")
    if r:
        r["doc"] = json.loads(r["doc"])
        r["changes"] = json.loads(r["changes"] or "[]")
    return r


# ---------------------------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------------------------
def _store_run(batch_id: int, result: dict, user: dict, keep_old: bool) -> int:
    summary = {"lines": len(result["lines"]), "requirements": len(result["requirements"]), "pos": len(result["pos"]),
               "routes": {}}
    for l in result["lines"]:
        summary["routes"][l["route"]] = summary["routes"].get(l["route"], 0) + 1
    if not keep_old:
        for r in db.q("SELECT id FROM runs WHERE batch_id=?", (batch_id,)):
            for t in ("run_lines", "run_reqs", "run_pos"):
                db.ex(f"DELETE FROM {t} WHERE run_id=?", (r["id"],))
            db.ex("DELETE FROM runs WHERE id=?", (r["id"],))
    else:
        db.ex("UPDATE runs SET is_latest=0 WHERE batch_id=?", (batch_id,))
    rid = db.ex("INSERT INTO runs(batch_id,created_at,created_by,policy_versions,summary,is_latest) VALUES(?,?,?,?,?,1)",
                (batch_id, now(), user["name"], ",".join(result["policy_versions_used"]), db.jdump(summary)))
    with db.conn() as c:
        c.executemany("INSERT INTO run_lines VALUES(?,?,?,?)", [(rid, l["pr_no"], l["pr_line"], db.jdump(l)) for l in result["lines"]])
        c.executemany("INSERT INTO run_reqs(run_id,group_id,data) VALUES(?,?,?)", [(rid, r["group_id"], db.jdump(r)) for r in result["requirements"]])
        c.executemany("INSERT INTO run_pos VALUES(?,?,?)", [(rid, p["po_id"], db.jdump(p)) for p in result["pos"]])
    return rid


def _rows_for_batch(batch_id: int, only_open: bool = False) -> list[dict]:
    sql = "SELECT * FROM pr_lines WHERE batch_id=?" + (" AND status='open'" if only_open else "") + " ORDER BY id"
    return db.q(sql, (batch_id,))


def process_rows(rows: list[dict], name: str, user: dict, kind: str = "csv") -> dict:
    """Store a PR batch and run the engine on it (API: POST /api/process-batch)."""
    ref, matcher = get_reference()
    bid = db.ex("INSERT INTO batches(name,kind,created_by,created_at) VALUES(?,?,?,?)", (name, kind, user["name"], now()))
    with db.conn() as c:
        c.executemany("INSERT INTO pr_lines(batch_id,pr_no,pr_line,created_at,requester,department,plant,item_code,description,quantity,uom,estimated_unit_price,need_by_date,urgency,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      [(bid, r.get("pr_no"), int(float(r.get("pr_line") or 1)), r.get("created_at"), r.get("requester"), r.get("department"), r.get("plant"), r.get("item_code"),
                        r.get("description"), r.get("quantity"), r.get("uom"), (float(r["estimated_unit_price"]) if str(r.get("estimated_unit_price") or "").strip() not in ("", "nan") else None),
                        r.get("need_by_date"), r.get("urgency"), "csv") for r in rows])
    result = run_engine(_rows_for_batch(bid), ref, approved_docs(), matcher)
    rid = _store_run(bid, result, user, keep_old=True)
    audit(user, "process_batch", "batch", bid, {"run_id": rid, "rows": len(rows), "policy_versions": result["policy_versions_used"]})
    return {"batch_id": bid, "run_id": rid, **result}


def reprocess_batch(batch_id: int, user: dict) -> dict:
    b = db.q1("SELECT * FROM batches WHERE id=?", (batch_id,))
    if not b:
        raise ValueError("unknown batch")
    ref, matcher = get_reference()
    rows = _rows_for_batch(batch_id, only_open=(b["kind"] == "live"))
    result = run_engine(rows, ref, approved_docs(), matcher)
    rid = _store_run(batch_id, result, user, keep_old=(b["kind"] != "live"))
    audit(user, "reprocess_batch", "batch", batch_id, {"run_id": rid, "policy_versions": result["policy_versions_used"]})
    return {"batch_id": batch_id, "run_id": rid, **result}


def add_chat_line(line: dict, user: dict) -> dict:
    """Create a PR line in the live queue and re-run the live batch so clubbing sees it."""
    bid = live_batch_id()
    n = db.q1("SELECT COUNT(DISTINCT pr_no) n FROM pr_lines WHERE batch_id=?", (bid,))["n"]
    pr_no = f"PR-26-C{n + 1:04d}"
    db.ex("INSERT INTO pr_lines(batch_id,pr_no,pr_line,created_at,requester,department,plant,item_code,description,quantity,uom,estimated_unit_price,need_by_date,urgency,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (bid, pr_no, 1, line["created_at"], user["name"], line.get("department") or "", line["plant"], line.get("item_code"), line["description"], line["quantity"],
           line.get("uom") or "", line.get("estimated_unit_price"), line["need_by_date"], line["urgency"], "chat"))
    audit(user, "pr_created_via_chat", "pr", pr_no, line)
    out = reprocess_batch(bid, user)
    mine = next((l for l in out["lines"] if l["pr_no"] == pr_no), None)
    req = next((r for r in out["requirements"] if [pr_no, 1] in r["line_keys"]), None)
    return {"pr_no": pr_no, "decision_line": mine, "requirement": req, "run_id": out["run_id"]}


def list_batches() -> list[dict]:
    out = []
    for b in db.q("SELECT * FROM batches ORDER BY id DESC"):
        r = db.q1("SELECT * FROM runs WHERE batch_id=? AND is_latest=1 ORDER BY id DESC LIMIT 1", (b["id"],))
        b["latest_run"] = r["id"] if r else None
        b["summary"] = json.loads(r["summary"]) if r else None
        b["policy_versions"] = r["policy_versions"] if r else None
        b["pr_lines"] = db.q1("SELECT COUNT(*) n FROM pr_lines WHERE batch_id=?", (b["id"],))["n"]
        b["pending"] = db.q1("SELECT COUNT(*) n FROM run_reqs WHERE run_id=? AND status='pending'", (r["id"],))["n"] if r else 0
        out.append(b)
    return out


def get_run(run_id: int) -> dict:
    run = db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
    if not run:
        raise ValueError("unknown run")
    reqs = []
    for r in db.q("SELECT * FROM run_reqs WHERE run_id=? ORDER BY group_id", (run_id,)):
        d = json.loads(r["data"])
        d.update(status=r["status"], doc_ref=r["doc_ref"], decided_by=r["decided_by"], decided_at=r["decided_at"])
        d["overrides"] = [{"comment": o["comment"], "by": o["by_user"], "at": o["at"], "new_decision": json.loads(o["new_decision"])} for o in db.q("SELECT * FROM overrides WHERE run_id=? AND group_id=?", (run_id, r["group_id"]))]
        reqs.append(d)
    lines = [json.loads(r["data"]) for r in db.q("SELECT data FROM run_lines WHERE run_id=? ORDER BY pr_no, pr_line", (run_id,))]
    pos = [json.loads(r["data"]) for r in db.q("SELECT data FROM run_pos WHERE run_id=? ORDER BY po_id", (run_id,))]
    run["summary"] = json.loads(run["summary"])
    return {"run": run, "requirements": reqs, "lines": lines, "pos": pos}


def export_csv(run_id: int) -> str:
    run = get_run(run_id)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for l in run["lines"]:
        w.writerow({k: l.get(k, "") for k in EXPORT_COLUMNS})
    return buf.getvalue()


def latest_run_for_batch(batch_id: int) -> int | None:
    r = db.q1("SELECT id FROM runs WHERE batch_id=? AND is_latest=1 ORDER BY id DESC LIMIT 1", (batch_id,))
    return r["id"] if r else None


# ---------------------------------------------------------------------------------------------
# Buyer actions
# ---------------------------------------------------------------------------------------------
def _next_doc_no(prefix: str) -> str:
    key = f"seq_{prefix}"
    n = int(db.meta_get(key, "0") or 0) + 1
    db.meta_set(key, str(n))
    return f"{prefix}-26-{n:04d}"


def _mark_lines_done(run_id: int, keys: list[list]) -> None:
    run = db.q1("SELECT batch_id FROM runs WHERE id=?", (run_id,))
    b = db.q1("SELECT kind FROM batches WHERE id=?", (run["batch_id"],))
    if b and b["kind"] == "live":
        for pr_no, pr_line in keys:
            db.ex("UPDATE pr_lines SET status='done' WHERE batch_id=? AND pr_no=? AND pr_line=?", (run["batch_id"], pr_no, pr_line))


def approve(run_id: int, group_id: str, user: dict) -> dict:
    rq = db.q1("SELECT * FROM run_reqs WHERE run_id=? AND group_id=?", (run_id, group_id))
    if not rq:
        raise ValueError("unknown requirement")
    if rq["status"] != "pending":
        raise ValueError(f"requirement is already {rq['status']}")
    d = json.loads(rq["data"])
    if d["decision"] == "PO":
        siblings = [json.loads(r["data"]) | {"_status": r["status"]} for r in db.q("SELECT * FROM run_reqs WHERE run_id=?", (run_id,))
                    if json.loads(r["data"]).get("po_id") == d["po_id"]]
        todo = [s for s in siblings if s["_status"] == "pending"]
        doc_no = _next_doc_no("PO")
        total = 0.0
        for s in todo:
            total += s["quantity"] * s["unit_price"]
            if s["route"] == "CONTRACT_PO" and s.get("contract_id"):
                db.ex("UPDATE contracts SET consumed_value = consumed_value + ? WHERE contract_id=?", (round(s["quantity"] * s["unit_price"], 2), s["contract_id"]))
            db.ex("UPDATE run_reqs SET status='approved', doc_ref=?, decided_by=?, decided_at=? WHERE run_id=? AND group_id=?", (doc_no, user["name"], now(), run_id, s["group_id"]))
            _mark_lines_done(run_id, s["line_keys"])
        db.ex("INSERT INTO orders VALUES(?,?,?,?,?,?,?)", (doc_no, "PO", run_id, d["po_id"], db.jdump({"supplier_id": d["supplier_id"], "plant": d["plant"], "route": d["route"], "total_value": round(total, 2),
                                                                                                         "requirements": [{"group_id": s["group_id"], "item_code": s["item_code"], "quantity": s["quantity"], "unit_price": s["unit_price"], "reason": s["reason"]} for s in todo]}),
                                                                      user["name"], now()))
        bump_data()  # contract consumption changed
        audit(user, "approve_po", "po", doc_no, {"run_id": run_id, "requirements": [s["group_id"] for s in todo], "total_value": round(total, 2)})
        return {"doc_no": doc_no, "kind": "PO", "requirements": [s["group_id"] for s in todo]}
    prefix = {"RFQ": "RFQ", "RFP": "RFP", "CAPEX_SOURCING": "CAPEX", "NEW_ITEM_SOURCING": "NIS"}.get(d["route"], "SR")
    doc_no = _next_doc_no(prefix)
    db.ex("UPDATE run_reqs SET status='approved', doc_ref=?, decided_by=?, decided_at=? WHERE run_id=? AND group_id=?", (doc_no, user["name"], now(), run_id, group_id))
    db.ex("INSERT INTO orders VALUES(?,?,?,?,?,?,?)", (doc_no, d["route"], run_id, group_id, db.jdump({"item_code": d["item_code"], "quantity": d["quantity"], "plant": d["plant"], "value": d["value"],
                                                                                                      "suggested_supplier_ids": d["suggested_supplier_ids"], "notes": d.get("notes", ""), "reason": d["reason"]}), user["name"], now()))
    _mark_lines_done(run_id, d["line_keys"])
    audit(user, "approve_sourcing", d["route"], doc_no, {"run_id": run_id, "group_id": group_id, "suppliers": d["suggested_supplier_ids"]})
    return {"doc_no": doc_no, "kind": d["route"], "requirements": [group_id]}


def override(run_id: int, group_id: str, new: dict, comment: str, user: dict) -> dict:
    if not comment or len(comment.strip()) < 5:
        raise ValueError("an override needs a written reason (at least 5 characters)")
    rq = db.q1("SELECT * FROM run_reqs WHERE run_id=? AND group_id=?", (run_id, group_id))
    if not rq or rq["status"] != "pending":
        raise ValueError("requirement not found or already actioned")
    d = json.loads(rq["data"])
    ref, _ = get_reference()
    decision = new.get("decision")
    if decision not in ("PO", "SOURCING_REQUEST"):
        raise ValueError("decision must be PO or SOURCING_REQUEST")
    sid = new.get("supplier_id")
    if decision == "PO":
        if not sid or sid not in ref.suppliers:
            raise ValueError("a PO override needs a valid supplier_id")
        if ref.suppliers[sid]["status"] != "Active":
            raise ValueError(f"supplier {sid} is {ref.suppliers[sid]['status']}: Blocked/Inactive suppliers can never receive a PO (policy 5.7), even by override")
    for s in new.get("suggested_supplier_ids", []):
        if s not in ref.suppliers or ref.suppliers[s]["status"] != "Active":
            raise ValueError(f"supplier {s} cannot be suggested (not Active)")
    new = {"decision": decision, "route": new.get("route") or ("SPOT_PO" if decision == "PO" else "RFQ"), "supplier_id": sid,
           "unit_price": float(new["unit_price"]) if new.get("unit_price") not in (None, "") else d.get("unit_price"),
           "suggested_supplier_ids": new.get("suggested_supplier_ids", [])}
    doc_no = _next_doc_no("PO" if decision == "PO" else new["route"][:5])
    db.ex("INSERT INTO overrides(run_id,group_id,original,new_decision,comment,by_user,at) VALUES(?,?,?,?,?,?,?)", (run_id, group_id, db.jdump({k: d[k] for k in ("decision", "route", "supplier_id", "unit_price", "suggested_supplier_ids", "reason", "clause", "policy_version")}), db.jdump(new), comment.strip(), user["name"], now()))
    db.ex("UPDATE run_reqs SET status='overridden', doc_ref=?, decided_by=?, decided_at=? WHERE run_id=? AND group_id=?", (doc_no, user["name"], now(), run_id, group_id))
    db.ex("INSERT INTO orders VALUES(?,?,?,?,?,?,?)", (doc_no, new["route"], run_id, group_id, db.jdump({"override": True, "comment": comment, **new, "item_code": d["item_code"], "quantity": d["quantity"], "plant": d["plant"], "value": d["value"]}), user["name"], now()))
    _mark_lines_done(run_id, d["line_keys"])
    audit(user, "override", "requirement", f"{run_id}/{group_id}", {"comment": comment, "new": new, "original_route": d["route"], "doc": doc_no})
    return {"doc_no": doc_no}


def list_orders() -> list[dict]:
    out = []
    for r in db.q("SELECT * FROM orders ORDER BY created_at DESC"):
        r["data"] = json.loads(r["data"])
        out.append(r)
    return out


# ---------------------------------------------------------------------------------------------
# Preferred suppliers
# ---------------------------------------------------------------------------------------------
def policy_on(date: dt.date) -> dict:
    docs = approved_docs()
    return P.version_for_date(docs, date)


def preferred_for(item_code: str, as_of: dt.date) -> dict:
    ref, _ = get_reference()
    pol = policy_on(as_of)
    if item_code not in ref.items:
        raise ValueError("unknown item")
    return {"item": ref.items[item_code], "as_of": as_of.isoformat(), "policy_version": pol["version"], "weights": pol["supplier_ranking"],
            "suppliers": preferred_list(ref, item_code, as_of, pol), "excluded": excluded_suppliers(ref, item_code, as_of, pol)}


def excluded_suppliers(ref: Reference, item_code: str, as_of: dt.date, pol: dict) -> list[dict]:
    look = pol["parameters"]["preferred_lookback_days"]
    sids = {p["supplier_id"] for p in ref.pos_in_window(item_code, as_of, look)} | {c["supplier_id"] for c in ref.contracts_for_item(item_code) if c["start_date"] <= as_of}
    return [{"supplier_id": s, "name": ref.suppliers[s]["name"], "status": ref.suppliers[s]["status"], "reason": f"{ref.suppliers[s]['status']} supplier excluded (policy 5.7 / 6.1)"}
            for s in sorted(sids) if s in ref.suppliers and not ref.supplier_ok(s, pol)]


def preferred_export_csv(as_of: dt.date, top: int = 3) -> str:
    ref, _ = get_reference()
    pol = policy_on(as_of)
    rows = export_preferred_rows(ref, as_of, pol, top)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["item_code", "rank", "supplier_id", "reference_price"], lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()
