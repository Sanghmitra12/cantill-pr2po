"""Read master data / contracts from CSV (+ PDF) into plain dict structures."""
from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile
from pathlib import Path

from .contract_pdf import parse_contract_pdf


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(str(s).strip()[:10])


def read_csv_text(text: str) -> list[dict]:
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(io.StringIO(text.lstrip("﻿")))]


def read_csv_bytes(b: bytes) -> list[dict]:
    return read_csv_text(b.decode("utf-8-sig", errors="replace"))


def classify(name: str, rows: list[dict]) -> str | None:
    """Work out what a CSV is from its header (so users can upload files with any name)."""
    cols = set(rows[0].keys()) if rows else set()
    if {"item_code", "item_type", "standard_price"} <= cols:
        return "items"
    if {"supplier_id", "status", "on_time_pct"} <= cols:
        return "suppliers"
    if {"po_number", "po_date", "supplier_id"} <= cols:
        return "po_history"
    if {"contract_id", "value_cap", "consumed_value"} <= cols:
        return "contract_register"
    if {"contract_id", "item_code", "rate"} <= cols:
        return "contract_lines"
    if cols == {"plant", "name"} or ({"plant", "name"} <= cols and len(cols) <= 3):
        return "plants"
    if {"pr_no", "pr_line", "created_at"} <= cols:
        return "pr_batch"
    return None


def collect_kit(files: dict[str, bytes]) -> dict:
    """files: {filename: bytes} (CSVs and contract PDFs, possibly from a zip). Returns parsed master data."""
    out: dict = {"items": [], "suppliers": [], "plants": [], "po_history": [], "contract_register": [], "contract_lines": [], "pdfs": {}, "unknown": []}
    for name, data in files.items():
        low = name.lower()
        if low.endswith(".csv"):
            rows = read_csv_bytes(data)
            kind = classify(name, rows)
            if kind and kind != "pr_batch":
                out[kind].extend(rows)
            elif kind is None:
                out["unknown"].append(name)
        elif low.endswith(".pdf") and "/contracts/" in ("/" + low.replace("\\", "/")) or (low.endswith(".pdf") and Path(low).name.startswith("rc-")):
            out["pdfs"][Path(name).name] = data
    return out


def files_from_zip(data: bytes) -> dict[str, bytes]:
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            if n.endswith("/") or "__MACOSX" in n:
                continue
            files[n] = z.read(n)
    return files


def files_from_dir(path: str | Path) -> dict[str, bytes]:
    base = Path(path)
    return {str(p.relative_to(base)): p.read_bytes() for p in base.rglob("*") if p.is_file() and p.suffix.lower() in (".csv", ".pdf")}


def build_reference_dicts(kit: dict):
    """Turn parsed kit into (items, suppliers, plants, po_history, contracts) dicts (dates parsed)."""
    items = {r["item_code"]: {**r, "standard_price": float(r["standard_price"])} for r in kit["items"]}
    suppliers = {r["supplier_id"]: {**r, "on_time_pct": float(r["on_time_pct"]), "quality_rating": float(r["quality_rating"])} for r in kit["suppliers"]}
    plants = {r["plant"]: r["name"] for r in kit["plants"]}
    pos = []
    for r in kit["po_history"]:
        pos.append({"po_number": r["po_number"], "po_date": _d(r["po_date"]), "supplier_id": r["supplier_id"], "item_code": r["item_code"],
                    "quantity": float(r["quantity"]), "uom": r.get("uom", ""), "unit_price": float(r["unit_price"]), "plant": r.get("plant", ""),
                    "delivered_on_time": str(r.get("delivered_on_time", "")).lower() in ("true", "1", "yes")})
    lines_by: dict[str, dict] = {}
    for r in kit["contract_lines"]:
        lines_by.setdefault(r["contract_id"], {})[r["item_code"]] = float(r["rate"])
    contracts = []
    for r in kit["contract_register"]:
        contracts.append({"contract_id": r["contract_id"], "supplier_id": r["supplier_id"], "supplier_name": r.get("supplier_name", ""),
                          "start_date": _d(r["start_date"]), "end_date": _d(r["end_date"]), "value_cap": float(r["value_cap"]),
                          "consumed_value": float(r["consumed_value"]), "lines": lines_by.get(r["contract_id"], {}), "source": "register"})
    known = {c["contract_id"] for c in contracts}
    pdf_contracts = []
    for name, data in kit["pdfs"].items():
        p = parse_contract_pdf(data, items)
        if p.get("contract_id") and p["contract_id"] not in known and not p["warnings"] and p["lines"]:
            pdf_contracts.append({"contract_id": p["contract_id"], "supplier_id": p["supplier_id"], "supplier_name": p.get("supplier_name") or "",
                                  "start_date": _d(p["start_date"]), "end_date": _d(p["end_date"]), "value_cap": float(p["value_cap"]),
                                  "consumed_value": 0.0, "lines": {l["item_code"]: l["rate"] for l in p["lines"]}, "source": "pdf", "pdf_name": name})
    return items, suppliers, plants, pos, contracts + pdf_contracts
