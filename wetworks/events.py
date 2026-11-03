"""Activity log: everything agents do lands here so the console can show it live."""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


class EventLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                source TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL
            )"""
        )
        self.db.commit()
        self.busy: set[str] = set()  # agents currently working (drives the console's status lights)

    def add(self, source: str, kind: str, text: str) -> int:
        """kind: info | start | done | warn | error"""
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO events (ts, source, kind, text) VALUES (?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), source, kind, text[:1000]),
            )
            self.db.commit()
            return cur.lastrowid

    def recent(self, after_id: int = 0, limit: int = 100) -> list[dict]:
        with self.lock:
            if after_id:
                rows = self.db.execute(
                    "SELECT id, ts, source, kind, text FROM events WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (after_id, limit),
                ).fetchall()
            else:  # first load: newest `limit`, returned oldest-first
                rows = self.db.execute(
                    "SELECT id, ts, source, kind, text FROM events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()[::-1]
        return [dict(zip(("id", "ts", "source", "kind", "text"), r)) for r in rows]

    def working(self, source: str):
        """Context manager: marks `source` busy and logs start/done/error around a block."""
        log = self

        class _Working:
            def __enter__(self):
                log.busy.add(source)
                return self

            def __exit__(self, exc_type, exc, tb):
                log.busy.discard(source)
                if exc is not None:
                    log.add(source, "error", f"{type(exc).__name__}: {exc}")
                return False

        return _Working()
