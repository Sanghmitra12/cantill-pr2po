"""Extract a rate contract from its signed PDF (header terms + Schedule A table)."""
from __future__ import annotations

import io
import re

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _num(s: str) -> float:
    return float(s.replace(",", "").replace("₹", "").strip())


def _iso(s: str) -> str | None:
    s = s.strip().rstrip(".,")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return s
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.match(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})$", s)
    if m and m.group(2).lower() in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return None


DATE_PAT = r"(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?,?\s+\d{4})"


def parse_contract_text(text: str, item_master: dict[str, dict] | None = None) -> dict:
    """Pure-text parser (so it can be unit-tested without a PDF)."""
    out: dict = {"warnings": []}
    m = re.search(r"Contract No\.?\s*([A-Z]{2}-\d{4}-\d{3})", text)
    out["contract_id"] = m.group(1) if m else None
    m = re.search(r"Supplier\s+ID\s+(S\d+)", text)
    out["supplier_id"] = m.group(1) if m else None
    m = re.search(r"valid from\s+" + DATE_PAT + r"\s+to\s+" + DATE_PAT, text, re.I)
    out["start_date"] = _iso(m.group(1)) if m else None
    out["end_date"] = _iso(m.group(2)) if m else None
    m = re.search(r"shall not exceed\s+INR\s+([\d,\.]+)", text, re.I)
    out["value_cap"] = _num(m.group(1)) if m else None
    m = re.search(r"\"the Buyer\"\),?\s*and\s+(.+?),\s+[A-Za-z ]+\s*\(Supplier\s+ID", text, re.S)
    out["supplier_name"] = re.sub(r"\s+", " ", m.group(1)).strip() if m else None
    lines = []
    for m in re.finditer(r"^\s*\d+\s+(ITM-\d{4})\s+(.+?)\s+([A-Z]{2,5})\s+([\d,]+\.?\d*)\s*$", text, re.M):
        code, desc, uom, rate = m.groups()
        lines.append({"item_code": code, "description": desc.strip(), "uom": uom, "rate": _num(rate)})
    out["lines"] = lines
    for k in ("contract_id", "supplier_id", "start_date", "end_date", "value_cap"):
        if not out.get(k):
            out["warnings"].append(f"could not read {k}")
    if not lines:
        out["warnings"].append("no Schedule A lines found")
    if item_master:
        for l in lines:
            if l["item_code"] not in item_master:
                out["warnings"].append(f"{l['item_code']} is not in the item master")
    return out


def parse_contract_pdf(data: bytes, item_master: dict[str, dict] | None = None) -> dict:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join((p.extract_text(layout=True) or "") for p in pdf.pages)
    if not text.strip():
        return {"warnings": ["PDF has no extractable text (scanned?). Enter the contract manually."], "lines": []}
    return parse_contract_text(text, item_master)
