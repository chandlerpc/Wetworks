"""Agent state that outlives a single run: approval requests and follow-ups."""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Approvals:
    """Actions agents want to take that leave Wetworks or can't be undone. Nothing here runs
    until Patrick approves it."""

    def __init__(self, path: Path, on_request: Callable[[dict], None] | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created TEXT NOT NULL, updated TEXT NOT NULL,
                agent TEXT NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL,
                summary TEXT NOT NULL, reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', result TEXT NOT NULL DEFAULT ''
            )""")
        self.db.commit()
        self.on_request = on_request
        self.executors: dict[str, Callable[..., object]] = {}  # tool name -> function to run on approval

    KEYS = ("id", "created", "updated", "agent", "tool", "args", "summary", "reason", "status", "result")

    def _row(self, r) -> dict:
        d = dict(zip(self.KEYS, r))
        d["args"] = json.loads(d["args"])
        return d

    def request(self, agent: str, tool: str, args: dict, summary: str, reason: str = "") -> dict:
        with self.lock:
            # Don't stack duplicates of the same pending request.
            dup = self.db.execute("SELECT * FROM approvals WHERE status = 'pending' AND tool = ? AND args = ?",
                                  (tool, json.dumps(args, sort_keys=True))).fetchone()
            if dup:
                return self._row(dup)
            cur = self.db.execute(
                "INSERT INTO approvals (created, updated, agent, tool, args, summary, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_now().isoformat(), _now().isoformat(), agent, tool, json.dumps(args, sort_keys=True),
                 summary[:400], reason[:400]))
            self.db.commit()
            row = self._row(self.db.execute("SELECT * FROM approvals WHERE id = ?", (cur.lastrowid,)).fetchone())
        if self.on_request:
            try:
                self.on_request(row)
            except Exception:
                pass
        return row

    def get(self, aid: int) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone()
        return self._row(r) if r else None

    def pending(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM approvals WHERE status = 'pending' ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def recent(self, limit: int = 30) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM approvals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def _set(self, aid: int, status: str, result: str = "") -> None:
        with self.lock:
            self.db.execute("UPDATE approvals SET status = ?, result = ?, updated = ? WHERE id = ?",
                            (status, result[:2000], _now().isoformat(), aid))
            self.db.commit()

    def decide(self, aid: int, approve: bool) -> dict:
        a = self.get(aid)
        if not a:
            raise KeyError(aid)
        if a["status"] != "pending":
            raise ValueError(f"Already {a['status']}")
        if not approve:
            self._set(aid, "declined")
            return self.get(aid)
        fn = self.executors.get(a["tool"])
        if fn is None:
            self._set(aid, "failed", "No way to run this action anymore")
            return self.get(aid)
        self._set(aid, "running")
        try:
            result = fn(**a["args"])
            self._set(aid, "done", str(result if result is not None else "Done"))
        except Exception as e:
            self._set(aid, "failed", f"{type(e).__name__}: {e}")
        return self.get(aid)


class Followups:
    """Notes agents leave for their future selves ("check mission 7 tomorrow")."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS followups (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, due TEXT NOT NULL,
                agent TEXT NOT NULL, note TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)""")
        self.db.commit()

    def add(self, agent: str, note: str, hours: float) -> dict:
        hours = max(0.25, min(float(hours), 24 * 30))
        due = (_now() + timedelta(hours=hours)).isoformat()
        with self.lock:
            cur = self.db.execute("INSERT INTO followups (created, due, agent, note) VALUES (?, ?, ?, ?)",
                                  (_now().isoformat(), due, agent, note.strip()[:400]))
            self.db.commit()
        return {"id": cur.lastrowid, "due": due, "note": note}

    def due(self, now: datetime | None = None) -> list[dict]:
        now = now or _now()
        with self.lock:
            rows = self.db.execute("SELECT id, due, agent, note FROM followups WHERE done = 0 AND due <= ? ORDER BY due",
                                   (now.isoformat(),)).fetchall()
        return [dict(zip(("id", "due", "agent", "note"), r)) for r in rows]

    def close(self, ids: list[int]) -> None:
        if not ids:
            return
        with self.lock:
            self.db.execute(f"UPDATE followups SET done = 1 WHERE id IN ({','.join('?' * len(ids))})", ids)
            self.db.commit()
