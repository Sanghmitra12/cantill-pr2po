"""SQLite access.  One short-lived connection per call; JSON blobs for engine output, columns for what we query."""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.environ.get("DATA_DIR", "./data"), "app.db"))
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS items(item_code TEXT PRIMARY KEY, description TEXT, uom TEXT, category TEXT, item_type TEXT, standard_price REAL);
CREATE TABLE IF NOT EXISTS suppliers(supplier_id TEXT PRIMARY KEY, name TEXT, city TEXT, categories TEXT, status TEXT, on_time_pct REAL, quality_rating REAL);
CREATE TABLE IF NOT EXISTS plants(plant TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS po_history(po_number TEXT, po_date TEXT, supplier_id TEXT, item_code TEXT, quantity REAL, uom TEXT, unit_price REAL, plant TEXT, delivered_on_time INTEGER);
CREATE INDEX IF NOT EXISTS ix_po_item ON po_history(item_code, po_date);
CREATE TABLE IF NOT EXISTS contracts(contract_id TEXT PRIMARY KEY, supplier_id TEXT, supplier_name TEXT, start_date TEXT, end_date TEXT,
  value_cap REAL, consumed_value REAL, source TEXT, pdf_name TEXT, added_by TEXT, added_at TEXT);
CREATE TABLE IF NOT EXISTS contract_lines(contract_id TEXT, item_code TEXT, description TEXT, uom TEXT, rate REAL, PRIMARY KEY(contract_id, item_code));
CREATE TABLE IF NOT EXISTS policies(id INTEGER PRIMARY KEY AUTOINCREMENT, version TEXT, effective_date TEXT, status TEXT, doc TEXT,
  source_kind TEXT, source_name TEXT, source_text TEXT, changes TEXT, notes TEXT, created_by TEXT, created_at TEXT, decided_by TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, kind TEXT, created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS pr_lines(id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER, pr_no TEXT, pr_line INTEGER, created_at TEXT, requester TEXT,
  department TEXT, plant TEXT, item_code TEXT, description TEXT, quantity REAL, uom TEXT, estimated_unit_price REAL, need_by_date TEXT, urgency TEXT,
  source TEXT, status TEXT DEFAULT 'open');
CREATE INDEX IF NOT EXISTS ix_pr_batch ON pr_lines(batch_id);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER, created_at TEXT, created_by TEXT, policy_versions TEXT, summary TEXT, is_latest INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS run_lines(run_id INTEGER, pr_no TEXT, pr_line INTEGER, data TEXT);
CREATE INDEX IF NOT EXISTS ix_rl_run ON run_lines(run_id);
CREATE TABLE IF NOT EXISTS run_reqs(run_id INTEGER, group_id TEXT, data TEXT, status TEXT DEFAULT 'pending', po_id TEXT, doc_ref TEXT,
  decided_by TEXT, decided_at TEXT, PRIMARY KEY(run_id, group_id));
CREATE TABLE IF NOT EXISTS run_pos(run_id INTEGER, po_id TEXT, data TEXT, PRIMARY KEY(run_id, po_id));
CREATE TABLE IF NOT EXISTS overrides(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, group_id TEXT, original TEXT, new_decision TEXT, comment TEXT, by_user TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS orders(doc_id TEXT PRIMARY KEY, kind TEXT, run_id INTEGER, group_id TEXT, data TEXT, created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user TEXT, role TEXT, action TEXT, entity TEXT, entity_id TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS chat_sessions(id TEXT PRIMARY KEY, user TEXT, state TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS chat_messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, ts TEXT);
"""


def ensure_dir():
    d = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(d, exist_ok=True)


@contextlib.contextmanager
def conn():
    ensure_dir()
    with _lock:
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def q(sql: str, args: tuple = ()) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def q1(sql: str, args: tuple = ()) -> dict | None:
    r = q(sql, args)
    return r[0] if r else None


def ex(sql: str, args: tuple = ()) -> int:
    with conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def jdump(o) -> str:
    return json.dumps(o, default=str, ensure_ascii=False)


def meta_get(key: str, default: str | None = None) -> str | None:
    r = q1("SELECT value FROM meta WHERE key=?", (key,))
    return r["value"] if r else default


def meta_set(key: str, value: str) -> None:
    ex("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
