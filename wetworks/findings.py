"""Findings store: what cells discover, shared with The Architect's briefs."""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class Finding:
    cell: str
    topic: str
    title: str
    summary: str
    url: str = ""
    importance: int = 3  # 1 = trivia, 5 = act today
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class FindingsStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS findings (
                ts TEXT NOT NULL,
                cell TEXT NOT NULL,
                topic TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                url TEXT NOT NULL,
                importance INTEGER NOT NULL,
                reported INTEGER NOT NULL DEFAULT 0
            )"""
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS findings_url ON findings(url)")
        self.db.commit()

    def seen_url(self, url: str, days: int = 14) -> bool:
        if not url:
            return False
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        row = self.db.execute("SELECT 1 FROM findings WHERE url = ? AND ts >= ? LIMIT 1", (url, cutoff)).fetchone()
        return row is not None

    def add(self, f: Finding) -> bool:
        """Store a finding unless its URL was already reported recently. Returns True if stored."""
        if self.seen_url(f.url):
            return False
        self.db.execute(
            "INSERT INTO findings (ts, cell, topic, title, summary, url, importance) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f.ts, f.cell, f.topic, f.title, f.summary, f.url, f.importance),
        )
        self.db.commit()
        return True

    def recent_titles(self, topic: str, days: int = 7, limit: int = 15) -> list[str]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.db.execute(
            "SELECT title FROM findings WHERE topic = ? AND ts >= ? ORDER BY ts DESC LIMIT ?", (topic, cutoff, limit)
        )
        return [r[0] for r in rows]

    def unreported(self, min_importance: int = 1) -> list[Finding]:
        rows = self.db.execute(
            "SELECT cell, topic, title, summary, url, importance, ts FROM findings "
            "WHERE reported = 0 AND importance >= ? ORDER BY importance DESC, ts DESC",
            (min_importance,),
        )
        return [Finding(cell=c, topic=t, title=ti, summary=s, url=u, importance=i, ts=ts) for c, t, ti, s, u, i, ts in rows]

    def mark_reported(self) -> None:
        self.db.execute("UPDATE findings SET reported = 1 WHERE reported = 0")
        self.db.commit()
