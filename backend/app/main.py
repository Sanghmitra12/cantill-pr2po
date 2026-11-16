"""FastAPI application: JSON API under /api, static frontend at /."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import chat, db, loaders, llm, policy as P, policy_nl, service
from .contract_pdf import parse_contract_pdf
from .engine import EXPORT_COLUMNS

app = FastAPI(title="Cantill PR→PO Assistant", version="1.0")
FRONTEND = Path(os.environ.get("FRONTEND_DIR", Path(__file__).resolve().parents[2] / "frontend"))

ROLES = {"requester": 1, "buyer": 2, "procurement": 3}


@app.on_event("startup")
def _startup() -> None:
    service.init_app()


def who(x_user: str | None, x_role: str | None) -> dict:
    return {"name": (x_user or "demo.user").strip()[:60], "role": (x_role or "requester").strip().lower()}


def need(user: dict, role: str) -> None:
    if ROLES.get(user["role"], 0) < ROLES[role]:
        raise HTTPException(403, f"This action needs the '{role}' role (you are '{user['role']}'). Switch role in the top bar.")


@app.exception_handler(ValueError)
async def _value_error(_, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(P.PolicyError)
async def _policy_error(_, exc):
    return JSONResponse({"detail": "Invalid policy: " + str(exc)}, status_code=422)


# ------------------------------------------------------------------ status / master data
@app.get("/api/status")
def status():
    return {"master": service.master_status(), "llm": llm.available(), "today": dt.date.today().isoformat(),
            "policies": [{"version": p["version"], "status": p["status"], "effective_date": p["effective_date"]} for p in service.policy_rows()]}


@app.post("/api/admin/load-master-data")
async def load_master(files: list[UploadFile] = File(...), x_user: str | None = Header(None), x_role: str | None = Header(None)):
    user = who(x_user, x_role)
    need(user, "procurement")
    bag: dict[str, bytes] = {}
    for f in files:
        data = await f.read()
        if f.filename.lower().endswith(".zip"):
            bag.update(loaders.files_from_zip(data))
        else:
            bag[f.filename] = data
    counts = service.load_master(bag, user)
    return {"loaded": counts, "master": service.master_status()}


@app.get("/api/items")
def items(q: str = ""):
    ref, matcher = service.get_reference()
    if q:
        return [{"item_code": c.item_code, "description": c.description, "score": c.score} for c in matcher.rank(q, 10)]
    return list(ref.items.values())


@app.get("/api/suppliers")
def suppliers():
    return list(service.get_reference()[0].suppliers.values())


# ------------------------------------------------------------------ chat
@app.post("/api/chat")
def chat_endpoint(body: dict = Body(...), x_user: str | None = Header(None), x_role: str | None = Header(None)):
    user = who(x_user, x_role)
    return chat.handle(body.get("session_id"), user, body.get("message", ""))


@app.get("/api/chat/{sid}")
def chat_history(sid: str):
    return {"history": chat.history(sid)}


# ------------------------------------------------------------------ batches / runs
def _rows_from_upload(data: bytes) -> list[dict]:
    rows = loaders.read_csv_bytes(data)
    if not rows or "pr_no" not in rows[0]:
        raise ValueError("This does not look like a PR batch CSV (needs pr_no, pr_line, created_at, plant, description, quantity, ...)")
    return rows


@app.post("/api/process-batch")
async def process_batch(file: UploadFile = File(...), format: str = Query("json"), name: str | None = Query(None),
                        x_user: str | None = Header(None), x_role: str | None = Header(None)):
    """Upload a PR batch CSV; returns decisions (json) or the export CSV (format=csv)."""
    user = who(x_user, x_role)
    need(user, "buyer")
    rows = _rows_from_upload(await file.read())
    out = service.process_rows(rows, name or file.filename, user)
    if format == "csv":
        return PlainTextResponse(service.export_csv(out["run_id"]), media_type="text/csv")
    return {"batch_id": out["batch_id"], "run_id": out["run_id"], "policy_versions": out["policy_versions_used"], "decisions": out["lines"], "pos": out["pos"]}


@app.get("/api/batches")
def batches():
    return service.list_batches()


@app.post("/api/batches/{batch_id}/reprocess")
def reprocess(batch_id: int, x_user: str | None = Header(None), x_role: str | None = Header(None)):
    user = who(x_user, x_role)
    need(user, "buyer")
    out = service.reprocess_batch(batch_id, user)
    return {"batch_id": batch_id, "run_id": out["run_id"], "policy_versions": out["policy_versions_used"]}


@app.get("/api/runs/{run_id}")
def run(run_id: int):
    return service.get_run(run_id)


@app.get("/api/runs/{run_id}/export.csv")
def run_export(run_id: int, name: str = "run"):
    return PlainTextResponse(service.export_csv(run_id), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


@app.post("/api/runs/{run_id}/requirements/{group_id}/approve")
def approve(run_id: int, group_id: str, x_user: str | None = Header(None), x_role: str | None = Header(None)):
    user = who(x_user, x_role)
    need(user, "buyer")
    return service.approve(run_id, group_id, user)


@app.post("/api/runs/{run_id}/requirements/{group_id}/override")
def override(run_id: int, group_id: str, body: dict = Body(...), x_user: str | None = Header(None), x_role: str | None = Header(None)):
    user = who(x_user, x_role)
    need(user, "buyer")
    return service.override(run_id, group_id, body, body.get("comment", ""), user)


@app.get("/api/orders")
def orders():
    return service.list_orders()


# ------------------------------------------------------------------ policies
@app.get("/api/policies")
def policies():
    return service.policy_rows()


@app.get("/api/policies/fields")
def policy_fields():
    return {"fields": P.FIELDS, "operators": P.OPERATORS, "modes": P.SUPPLIER_MODES, "routes": P.KNOWN_ROUTES}


def _meta() -> dict:
    ref, _ = service.get_reference()
    return {"plants": ref.plants, "categories": sorted({i["category"] for i in ref.items.values()})}


def _interpret_and_draft(text: str, source_kind: str, source_name: str, user: dict, effective_date: str | None, version: str | None) -> dict:
    d = service.latest_draft()
    base = d["doc"] if d else (service.policy_on(dt.date.today()) or service.approved_docs()[-1])
    existing = [r["version"] for r in service.policy_rows()]
    res = policy_nl.interpret(text, base, _meta(), existing)
    doc = res["doc"]
    if effective_date:
        doc["effective_date"] = effective_date
    if version:
        doc["version"] = version
    res["changes"] = P.diff_policies(service.policy_on(dt.date.fromisoformat(doc["effective_date"])) or base, doc)
    if d:  # refine the existing draft instead of piling up drafts
        service.update_draft(d["id"], doc, user)
        pid = d["id"]
    else:
        pid = service.create_draft(doc, user, source_kind, source_name, text, res["changes"], "; ".join(res["notes"]))["id"]
    return {"id": pid, "version": doc["version"], "effective_date": doc["effective_date"], "method": res["method"], "changes": res["changes"],
            "notes": res["notes"], "unparsed": res["unparsed"], "extended_from_existing_draft": bool(d)}


@app.post("/api/policies/upload")
async def policy_upload(file: UploadFile = File(...), effective_date: str | None = Form(None), version: str | None = Form(None),
                        x_user: str | None = Header(None), x_role: str | None = Header(None)):
    """Upload a policy document (PDF/DOCX/TXT/MD) or a policy JSON; creates a DRAFT with a plain-language diff."""
    user = who(x_user, x_role)
    need(user, "procurement")
    data = await file.read()
    name = file.filename or "upload"
    if name.lower().endswith(".json"):
        doc = json.loads(data.decode("utf-8-sig"))
        base = service.policy_on(dt.date.today())
        res = service.create_draft(doc, user, "json", name, "", None, "Uploaded as complete policy JSON")
        return {"id": res["id"], "version": res["version"], "changes": res["changes"], "method": "json", "notes": [], "unparsed": []}
    text = policy_nl.extract_text(name, data)
    if not text.strip():
        raise ValueError("Could not read any text from the file. Paste the amendment text instead.")
    return _interpret_and_draft(text, "document", name, user, effective_date, version)


@app.post("/api/policies/describe")
def policy_describe(body: dict = Body(...), x_user: str | None = Header(None), x_role: str | None = Header(None)):
    """Describe a change in words (chat-style). Refines the open draft if there is one."""
    user = who(x_user, x_role)
    need(user, "procurement")
    text = (body.get("text") or "").strip()
    if not text:
        raise ValueError("Describe the change first")
    return _interpret_and_draft(text, "chat", "policy chat", user, body.get("effective_date"), body.get("version"))


@app.put("/api/policies/{pid}")
def policy_edit(pid: int, body: dict = Body(...), x_user: str | None = Header(None), x_role: str | None = Header(None)):
    user = who(x_user, x_role)
    need(user, "procurement")
    return service.update_draft(pid, body["doc"], user)


@app.post("/api/policies/{pid}/approve")
def policy_approve(pid: int, body: dict = Body(default={}), x_user: str | None = Header(None), x_role: str | None = Header(None)):
    user = who(x_user, x_role)
    need(user, "procurement")
    return service.decide_policy(pid, True, user, body.get("note", ""))


@app.post("/api/policies/{pid}/reject")
def policy_reject(pid: int, body: dict = Body(default={}), x_user: str | None = Header(None), x_role: str | None = Header(None)):
    user = who(x_user, x_role)
    need(user, "procurement")
    return service.decide_policy(pid, False, user, body.get("note", ""))


@app.get("/api/policies/{pid}/diff")
def policy_diff(pid: int, against: int | None = None):
    rows = {r["id"]: r for r in service.policy_rows()}
    if pid not in rows:
        raise HTTPException(404)
    new = rows[pid]["doc"]
    base = rows[against]["doc"] if against else (service.policy_on(dt.date.fromisoformat(new["effective_date"] )) or new)
    return {"changes": P.diff_policies(base, new)}


# ------------------------------------------------------------------ contracts
@app.get("/api/contracts")
def contracts():
    out = []
    ref, _ = service.get_reference()
    for c in ref.contracts:
        out.append({"contract_id": c["contract_id"], "supplier_id": c["supplier_id"], "supplier_name": c.get("supplier_name") or ref.suppliers.get(c["supplier_id"], {}).get("name", ""),
                    "supplier_status": ref.suppliers.get(c["supplier_id"], {}).get("status"), "start_date": c["start_date"].isoformat(), "end_date": c["end_date"].isoformat(),
                    "value_cap": c["value_cap"], "consumed_value": c["consumed_value"], "source": c.get("source"), "lines": [{"item_code": k, "rate": v, "description": ref.items.get(k, {}).get("description", "")} for k, v in c["lines"].items()]})
    return sorted(out, key=lambda c: c["contract_id"])


@app.post("/api/contracts/parse-pdf")
async def contract_parse(file: UploadFile = File(...)):
    """Extract a contract from a PDF for review (nothing is saved until confirmed)."""
    ref, _ = service.get_reference()
    p = parse_contract_pdf(await file.read(), ref.items)
    p["pdf_name"] = file.filename
    return p


@app.post("/api/contracts")
def contract_save(body: dict = Body(...), x_user: str | None = Header(None), x_role: str | None = Header(None)):
    user = who(x_user, x_role)
    need(user, "procurement")
    return service.save_contract(body, user, source="pdf_upload" if body.get("pdf_name") else "manual", pdf_name=body.get("pdf_name"))


# ------------------------------------------------------------------ preferred suppliers
@app.get("/api/preferred/{item_code}")
def preferred(item_code: str, as_of: str | None = None):
    return service.preferred_for(item_code.upper(), dt.date.fromisoformat(as_of) if as_of else dt.date.today())


@app.get("/api/preferred-export.csv")
def preferred_export(as_of: str | None = None, top: int = 3, name: str = "preferred"):
    return PlainTextResponse(service.preferred_export_csv(dt.date.fromisoformat(as_of) if as_of else dt.date.today(), top), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


# ------------------------------------------------------------------ audit
@app.get("/api/audit")
def audit(limit: int = 200):
    rows = db.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["detail"] = json.loads(r["detail"] or "{}")
    return rows


# ------------------------------------------------------------------ frontend
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND / "index.html")
