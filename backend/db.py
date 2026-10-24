"""SQLite storage for every record. The only module that writes to disk.

Records are stored as JSON (from schemas.to_dict) with a few indexed columns. Uniqueness
rules that protect the system live here:
  readings       (session_id, seq)        a packet is stored once
  plans          plan_id, version         each plan version is immutable once approved
  notifications  idempotency_key          re-approving a plan cannot message twice
  progress       event_id                 a repeated click is recorded once
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from backend.schemas import to_dict

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
  session_id TEXT NOT NULL, seq INTEGER NOT NULL, node_id TEXT NOT NULL,
  sample_time TEXT NOT NULL, simulated INTEGER NOT NULL, json TEXT NOT NULL,
  PRIMARY KEY (session_id, seq));
CREATE INDEX IF NOT EXISTS readings_node_time ON readings(node_id, sample_time);
CREATE TABLE IF NOT EXISTS records (
  kind TEXT NOT NULL, id TEXT NOT NULL, created_at TEXT NOT NULL, json TEXT NOT NULL,
  PRIMARY KEY (kind, id));
CREATE INDEX IF NOT EXISTS records_kind_time ON records(kind, created_at);
CREATE TABLE IF NOT EXISTS plans (
  plan_id TEXT PRIMARY KEY, version INTEGER UNIQUE NOT NULL, state TEXT NOT NULL,
  created_at TEXT NOT NULL, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assignments (
  assignment_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, household_id TEXT NOT NULL,
  state TEXT NOT NULL, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS progress (
  event_id TEXT PRIMARY KEY, household_id TEXT, team_id TEXT, kind TEXT NOT NULL,
  time TEXT NOT NULL, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifications (
  message_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, household_id TEXT NOT NULL,
  plan_id TEXT NOT NULL, status TEXT NOT NULL, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _iso(v) -> str:
    return v.isoformat() if isinstance(v, datetime) else str(v)


def _as_dict(obj) -> dict:
    return obj if isinstance(obj, dict) else to_dict(obj)


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)  # autocommit; transaction() opens BEGIN
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self._conn.close()

    def transaction(self):
        """`with db.transaction():` groups several writes; rolls back on error."""
        db = self

        class _Tx:
            def __enter__(self_inner):
                db._lock.acquire()
                db._conn.execute("BEGIN")
                return db

            def __exit__(self_inner, exc_type, exc, tb):
                try:
                    if exc_type is None:
                        db._conn.commit()
                    else:
                        db._conn.rollback()
                finally:
                    db._lock.release()
                return False
        return _Tx()

    def _write(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, args).rowcount

    def _rows(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            return [json.loads(r[0]) for r in self._conn.execute(sql, args).fetchall()]

    # ---------------------------------------------------------------- readings
    def save_reading(self, reading) -> bool:
        """True if stored, False if this (session, seq) was already stored."""
        d = _as_dict(reading)
        n = self._write("INSERT OR IGNORE INTO readings VALUES (?,?,?,?,?,?)",
                        (d["session_id"], d["seq"], d["node_id"], d["sample_time_utc"],
                         int(bool(d.get("simulated"))), json.dumps(d)))
        return n == 1

    def readings_since(self, node_id: str, since: datetime) -> list:
        return self._rows("SELECT json FROM readings WHERE node_id=? AND sample_time>=? ORDER BY sample_time",
                          (node_id, since.isoformat()))

    def reading_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

    # ---------------------------------------------------------------- generic records
    def put(self, kind: str, rec_id: str, obj, created_at: Optional[datetime] = None) -> None:
        d = _as_dict(obj)
        ts = created_at or d.get("created_at_utc") or d.get("fetched_at_utc") or ""
        self._write("INSERT OR REPLACE INTO records VALUES (?,?,?,?)", (kind, rec_id, _iso(ts), json.dumps(d)))

    def get(self, kind: str, rec_id: str) -> Optional[dict]:
        rows = self._rows("SELECT json FROM records WHERE kind=? AND id=?", (kind, rec_id))
        return rows[0] if rows else None

    def all(self, kind: str) -> list:
        return self._rows("SELECT json FROM records WHERE kind=? ORDER BY id", (kind,))

    def latest(self, kind: str) -> Optional[dict]:
        rows = self._rows("SELECT json FROM records WHERE kind=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (kind,))
        return rows[0] if rows else None

    def recent(self, kind: str, limit: int = 20) -> list:
        return self._rows("SELECT json FROM records WHERE kind=? ORDER BY created_at DESC, rowid DESC LIMIT ?", (kind, limit))

    # ---------------------------------------------------------------- plans
    def next_plan_version(self) -> int:
        with self._lock:
            v = self._conn.execute("SELECT MAX(version) FROM plans").fetchone()[0]
            return (v or 0) + 1

    def save_plan(self, plan) -> None:
        d = _as_dict(plan)
        self._write("INSERT OR REPLACE INTO plans VALUES (?,?,?,?,?)",
                    (d["plan_id"], d["version"], d["state"], d["created_at_utc"], json.dumps(d)))

    def get_plan(self, plan_id: str) -> Optional[dict]:
        rows = self._rows("SELECT json FROM plans WHERE plan_id=?", (plan_id,))
        return rows[0] if rows else None

    def latest_plan(self, state: Optional[str] = None) -> Optional[dict]:
        if state:
            rows = self._rows("SELECT json FROM plans WHERE state=? ORDER BY version DESC LIMIT 1", (state,))
        else:
            rows = self._rows("SELECT json FROM plans ORDER BY version DESC LIMIT 1")
        return rows[0] if rows else None

    # ---------------------------------------------------------------- assignments (accepted commitments)
    def save_assignment(self, a) -> None:
        d = _as_dict(a)
        self._write("INSERT OR REPLACE INTO assignments VALUES (?,?,?,?,?)",
                    (d["assignment_id"], d["team_id"], d["household_id"], d["state"], json.dumps(d)))

    def assignments(self, states: Optional[tuple] = None) -> list:
        if states:
            q = ",".join("?" * len(states))
            return self._rows(f"SELECT json FROM assignments WHERE state IN ({q})", tuple(states))
        return self._rows("SELECT json FROM assignments")

    def get_assignment(self, assignment_id: str) -> Optional[dict]:
        rows = self._rows("SELECT json FROM assignments WHERE assignment_id=?", (assignment_id,))
        return rows[0] if rows else None

    # ---------------------------------------------------------------- progress
    def add_progress(self, ev) -> bool:
        d = _as_dict(ev)
        n = self._write("INSERT OR IGNORE INTO progress VALUES (?,?,?,?,?,?)",
                        (d["event_id"], d.get("household_id"), d.get("team_id"), d["kind"], d["time_utc"], json.dumps(d)))
        return n == 1

    def progress(self, household_id: Optional[str] = None) -> list:
        if household_id:
            return self._rows("SELECT json FROM progress WHERE household_id=? ORDER BY time", (household_id,))
        return self._rows("SELECT json FROM progress ORDER BY time")

    # ---------------------------------------------------------------- notifications
    def add_notification(self, n) -> bool:
        d = _as_dict(n)
        cnt = self._write("INSERT OR IGNORE INTO notifications VALUES (?,?,?,?,?,?)",
                          (d["message_id"], d["idempotency_key"], d["household_id"], d["plan_id"], d["status"], json.dumps(d)))
        return cnt == 1

    def update_notification(self, n) -> None:
        d = _as_dict(n)
        self._write("UPDATE notifications SET status=?, json=? WHERE message_id=?", (d["status"], json.dumps(d), d["message_id"]))

    def notifications(self, household_id: Optional[str] = None) -> list:
        if household_id:
            return self._rows("SELECT json FROM notifications WHERE household_id=? ORDER BY rowid", (household_id,))
        return self._rows("SELECT json FROM notifications ORDER BY rowid")

    # ---------------------------------------------------------------- small settings
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            r = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        self._write("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def reset_runtime(self) -> None:
        """Clear readings, assessments, plans and progress; keep imported households and zones."""
        with self.transaction():
            for t in ("readings", "plans", "assignments", "progress", "notifications", "kv"):
                self._conn.execute(f"DELETE FROM {t}")
            self._conn.execute("DELETE FROM records WHERE kind IN ('weather','assessment','needs')")
