"""SQLite storage for discovered listings, pipeline runs and per-stage log events."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from ..settings import data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT,
    url TEXT NOT NULL UNIQUE,
    fingerprint TEXT,
    title TEXT, property_type TEXT, price INTEGER, first_price INTEGER, location TEXT, state TEXT,
    posted_at TEXT, posted_text TEXT, advertiser_type TEXT, advertiser_name TEXT, description TEXT,
    agent_indicators TEXT, owner_wording TEXT,
    owner_level TEXT, owner_score INTEGER, opportunity_score INTEGER, opportunity_reasons TEXT,
    status TEXT NOT NULL,              -- new | reviewed | rejected_agent | rejected_filter
    rejection_reason TEXT,
    duplicate_of INTEGER,
    first_seen TEXT, last_seen TEXT, last_run INTEGER, reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS leads_fp ON leads(fingerprint);
CREATE INDEX IF NOT EXISTS leads_status ON leads(status);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, started_at TEXT, finished_at TEXT, params TEXT,
    status TEXT,                       -- running | ok | zero_results | failed
    counts TEXT,                       -- JSON: per-stage counters
    diagnosis TEXT                     -- one sentence: where it stopped and why
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER, ts TEXT, stage TEXT, level TEXT, message TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id);
"""


def db_path() -> Path:
    return data_dir() / "listingai.db"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(str(path or db_path()), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(SCHEMA)
        yield con
        con.commit()
    finally:
        con.close()


def fingerprint(title: str, price: Optional[int], location: str) -> str:
    """Same title, price and area = same listing reposted under a new link."""
    t = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    loc = re.sub(r"[^a-z0-9]+", " ", (location or "").lower()).strip()
    return f"{t}|{price or ''}|{loc}"


def lead_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("agent_indicators", "owner_wording", "opportunity_reasons"):
        d[k] = json.loads(d[k]) if d.get(k) else []
    return d


def get_by_url(con: sqlite3.Connection, url: str) -> Optional[sqlite3.Row]:
    return con.execute("SELECT * FROM leads WHERE url = ?", (url,)).fetchone()


def get_by_fingerprint(con: sqlite3.Connection, fp: str) -> Optional[sqlite3.Row]:
    return con.execute("SELECT * FROM leads WHERE fingerprint = ? AND duplicate_of IS NULL ORDER BY id LIMIT 1",
                       (fp,)).fetchone()


def insert_lead(con: sqlite3.Connection, lead: dict[str, Any]) -> int:
    row = dict(lead)
    for k in ("agent_indicators", "owner_wording", "opportunity_reasons"):
        row[k] = json.dumps(row.get(k) or [], ensure_ascii=False)
    cols = ", ".join(row)
    cur = con.execute(f"INSERT INTO leads ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    return int(cur.lastrowid)


def update_lead(con: sqlite3.Connection, lead_id: int, changes: dict[str, Any]) -> None:
    row = dict(changes)
    for k in ("agent_indicators", "owner_wording", "opportunity_reasons"):
        if k in row:
            row[k] = json.dumps(row[k] or [], ensure_ascii=False)
    sets = ", ".join(f"{k} = ?" for k in row)
    con.execute(f"UPDATE leads SET {sets} WHERE id = ?", (*row.values(), lead_id))


def set_status(lead_id: int, status: str, path: Optional[Path] = None) -> None:
    with connect(path) as con:
        extra = ", reviewed_at = ?" if status == "reviewed" else ""
        params = (status, now_iso(), lead_id) if extra else (status, lead_id)
        con.execute(f"UPDATE leads SET status = ?{extra} WHERE id = ?", params)


TABS = {
    # tab: (SQL condition, ordering)
    "new": ("status = 'new' AND duplicate_of IS NULL", "owner_score DESC, opportunity_score DESC, id DESC"),
    "priority": ("status = 'new' AND duplicate_of IS NULL AND owner_score >= 80 AND opportunity_score >= 60",
                 "opportunity_score DESC, owner_score DESC"),
    "reviewed": ("status = 'reviewed'", "reviewed_at DESC"),
    "rejected_agent": ("status = 'rejected_agent' AND duplicate_of IS NULL", "last_seen DESC, id DESC"),
    "rejected_filter": ("status = 'rejected_filter' AND duplicate_of IS NULL", "last_seen DESC, id DESC"),
}


def list_leads(tab: str, limit: int = 300, path: Optional[Path] = None) -> list[dict]:
    where, order = TABS[tab]
    with connect(path) as con:
        return [lead_dict(r) for r in con.execute(f"SELECT * FROM leads WHERE {where} ORDER BY {order} LIMIT ?", (limit,))]


def tab_counts(path: Optional[Path] = None) -> dict[str, int]:
    with connect(path) as con:
        return {tab: con.execute(f"SELECT COUNT(*) FROM leads WHERE {where}").fetchone()[0]
                for tab, (where, _) in TABS.items()}


def recent_runs(limit: int = 15, path: Optional[Path] = None) -> list[dict]:
    with connect(path) as con:
        out = []
        for r in con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)):
            d = dict(r)
            d["counts"] = json.loads(d["counts"] or "{}")
            out.append(d)
        return out


def run_events(run_id: int, path: Optional[Path] = None) -> list[dict]:
    with connect(path) as con:
        return [dict(r) for r in con.execute("SELECT * FROM events WHERE run_id = ? ORDER BY id", (run_id,))]
