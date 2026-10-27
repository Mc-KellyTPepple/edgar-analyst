"""SQLite store for annual facts, plus a hardened read-only query helper.

`run_readonly_query` is what the agent's SQL tool will call. Model-written SQL
is untrusted, so the connection is opened read-only, an authorizer allows only
SELECT-type operations, only one statement runs, rows are capped, and a time
limit aborts runaway queries.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE companies(
  cik INTEGER PRIMARY KEY,
  ticker TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL
);
CREATE TABLE facts(
  cik INTEGER NOT NULL,
  metric TEXT NOT NULL,
  period_start TEXT,
  period_end TEXT NOT NULL,
  value REAL NOT NULL,           -- US dollars
  unit TEXT NOT NULL,
  accession TEXT NOT NULL,       -- filing the value comes from (for citations)
  form TEXT NOT NULL,
  filed TEXT NOT NULL,
  tag TEXT NOT NULL,
  PRIMARY KEY (cik, metric, period_end)
);
CREATE TABLE filings(
  cik INTEGER NOT NULL,
  accession TEXT PRIMARY KEY,
  form TEXT NOT NULL,
  filed TEXT NOT NULL,
  report_date TEXT,
  primary_doc TEXT
);
CREATE VIEW financials AS
  SELECT c.ticker, c.name, f.metric, f.period_start, f.period_end, f.value, ROUND(f.value / 1e6, 3) AS value_musd, f.accession
  FROM facts f JOIN companies c USING (cik);
"""


class QueryError(ValueError):
    """The query was rejected or failed."""


def build_db(path: Path, companies: list[tuple[int, str, str]], facts_by_cik: dict, filings_by_cik: dict | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO companies VALUES (?,?,?)", companies)
        for cik, facts in facts_by_cik.items():
            conn.executemany(
                "INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(cik, f.metric, f.period_start, f.period_end, f.value, f.unit, f.accession, f.form, f.filed, f.tag) for f in facts],
            )
        for cik, filings in (filings_by_cik or {}).items():
            conn.executemany(
                "INSERT OR IGNORE INTO filings VALUES (?,?,?,?,?,?)",
                [(cik, x["accession"], x["form"], x["filed"], x["report_date"], x["primary_doc"]) for x in filings],
            )
        conn.commit()
    finally:
        conn.close()


def open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


def _authorizer(action, arg1, arg2, dbname, source):
    if action not in _ALLOWED:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in {"load_extension", "readfile", "writefile"}:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def run_readonly_query(conn: sqlite3.Connection, sql: str, max_rows: int = 200, timeout_s: float = 5.0) -> dict:
    """Run one SELECT. Returns {'columns': [...], 'rows': [[...]], 'truncated': bool}."""
    deadline = time.monotonic() + timeout_s
    conn.set_authorizer(_authorizer)
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows + 1)
        return {
            "columns": columns,
            "rows": [list(r) for r in rows[:max_rows]],
            "truncated": len(rows) > max_rows,
        }
    except (sqlite3.Error, sqlite3.Warning) as exc:
        raise QueryError(str(exc)) from exc
    finally:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
