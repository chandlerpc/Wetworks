"""Mission queue: on-demand work handed to Splinter teams."""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("queued", "running", "done", "failed", "cancelled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MissionStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS missions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created TEXT NOT NULL,
                updated TEXT NOT NULL,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                request TEXT NOT NULL,
                status TEXT NOT NULL,
                steps TEXT NOT NULL DEFAULT '[]',
                result TEXT NOT NULL DEFAULT '',
                sources TEXT NOT NULL DEFAULT '[]'
            )"""
        )
        self.db.commit()
        self.cancel_requested: set[int] = set()

    def _row(self, r) -> dict:
        keys = ("id", "created", "updated", "source", "title", "request", "status", "steps", "result", "sources")
        d = dict(zip(keys, r))
        d["steps"] = json.loads(d["steps"])
        d["sources"] = json.loads(d["sources"])
        return d

    def create(self, title: str, request: str, source: str = "console") -> dict:
        title = (title or request).strip()[:120]
        request = request.strip()[:4000]
        if not request:
            raise ValueError("A mission needs a request.")
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO missions (created, updated, source, title, request, status) VALUES (?, ?, ?, ?, ?, 'queued')",
                (_now(), _now(), source, title, request),
            )
            self.db.commit()
            mid = cur.lastrowid
        return self.get(mid)

    def get(self, mid: int) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM missions WHERE id = ?", (mid,)).fetchone()
        return self._row(r) if r else None

    def recent(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM missions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = [self._row(r) for r in rows]
        for m in out:  # keep list payloads light
            m["result_preview"] = m["result"][:240]
            del m["result"], m["steps"]
        return out

    def next_queued(self) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM missions WHERE status = 'queued' ORDER BY id ASC LIMIT 1").fetchone()
        return self._row(r) if r else None

    def set_status(self, mid: int, status: str, result: str | None = None, sources: list | None = None) -> None:
        assert status in STATUSES
        with self.lock:
            if result is None:
                self.db.execute("UPDATE missions SET status = ?, updated = ? WHERE id = ?", (status, _now(), mid))
            else:
                self.db.execute(
                    "UPDATE missions SET status = ?, result = ?, sources = ?, updated = ? WHERE id = ?",
                    (status, result, json.dumps(sources or []), _now(), mid),
                )
            self.db.commit()

    def add_step(self, mid: int, text: str) -> None:
        with self.lock:
            r = self.db.execute("SELECT steps FROM missions WHERE id = ?", (mid,)).fetchone()
            if not r:
                return
            steps = json.loads(r[0])
            steps.append({"ts": _now(), "text": text[:500]})
            self.db.execute("UPDATE missions SET steps = ?, updated = ? WHERE id = ?", (json.dumps(steps), _now(), mid))
            self.db.commit()

    def cancel(self, mid: int) -> bool:
        m = self.get(mid)
        if not m or m["status"] not in ("queued", "running"):
            return False
        if m["status"] == "queued":
            self.set_status(mid, "cancelled")
        else:
            self.cancel_requested.add(mid)  # the worker stops at its next step
        return True

    def recover(self) -> int:
        """After a restart, missions left 'running' go back to the queue."""
        with self.lock:
            cur = self.db.execute("UPDATE missions SET status = 'queued', updated = ? WHERE status = 'running'", (_now(),))
            self.db.commit()
            return cur.rowcount
