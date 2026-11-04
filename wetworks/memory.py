"""Wetworks memory: what the agents know about Patrick and his world.

Stored in SQLite with full-text search (no extra model, no GPU). Three ways in:
  - the profile file (config/profile.md), re-read on start; edit it any time
  - "remember" items from chat (C.L.a.i.R / The Architect)
  - a nightly Sleeper that pulls durable facts out of the day's chats and missions
Patrick can see, search, add and forget anything from the console.
"""

import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

KINDS = ("profile", "preference", "fact", "project", "person", "decision")
STOPWORDS = set("""a an and are as at be but by can could did do does for from had has have he her his how i if in
into is it its me my no not of on or our she so than that the their them then there these they this to too up us
was we were what when where which who why will with would you your about just like get got want need please tell
know think make let lets also really okay ok hey hi clair claire""".split())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


class Memory:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                norm TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, content='memories', content_rowid='id');
            CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
            END;
            """
        )
        self.db.commit()

    KEYS = ("id", "created", "kind", "text", "source", "pinned")

    def _rows(self, rows) -> list[dict]:
        return [dict(zip(self.KEYS, r)) for r in rows]

    # ---- writing --------------------------------------------------------------
    def add(self, text: str, kind: str = "fact", source: str = "chat", pinned: bool = False) -> dict | None:
        """Store a memory. Returns None for empty text or an exact duplicate."""
        text = re.sub(r"\s+", " ", (text or "").strip())[:500]
        if len(text) < 3:
            return None
        kind = kind if kind in KINDS else "fact"
        with self.lock:
            try:
                cur = self.db.execute(
                    "INSERT INTO memories (created, kind, text, norm, source, pinned) VALUES (?, ?, ?, ?, ?, ?)",
                    (_now().isoformat(), kind, text, _norm(text), source[:40], int(pinned)),
                )
            except sqlite3.IntegrityError:
                return None
            self.db.commit()
            r = self.db.execute("SELECT id, created, kind, text, source, pinned FROM memories WHERE id = ?",
                                (cur.lastrowid,)).fetchone()
        return self._rows([r])[0]

    def forget(self, mid: int) -> bool:
        with self.lock:
            cur = self.db.execute("DELETE FROM memories WHERE id = ?", (mid,))
            self.db.commit()
            return cur.rowcount > 0

    def sync_profile(self, path: Path) -> int:
        """Load the profile file as pinned memories, replacing the previous profile import."""
        if not path.exists():
            return 0
        lines = []
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if line.startswith(("- ", "* ")):
                lines.append(line[2:].strip())
        with self.lock:
            self.db.execute("DELETE FROM memories WHERE source = 'profile'")
            self.db.commit()
        added = 0
        for line in lines:
            if self.add(line, kind="profile", source="profile", pinned=True):
                added += 1
        return added

    # ---- reading --------------------------------------------------------------
    def pinned(self, limit: int = 40) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id, created, kind, text, source, pinned FROM memories WHERE pinned = 1 ORDER BY id LIMIT ?",
                (limit,)).fetchall()
        return self._rows(rows)

    def search(self, query: str, limit: int = 8, include_pinned: bool = False) -> list[dict]:
        words = [w for w in re.findall(r"[a-z0-9]{3,}", (query or "").lower()) if w not in STOPWORDS][:12]
        if not words:
            return []
        match = " OR ".join(f'"{w}"*' for w in words)
        pin = "" if include_pinned else "AND m.pinned = 0"
        with self.lock:
            rows = self.db.execute(
                f"SELECT m.id, m.created, m.kind, m.text, m.source, m.pinned FROM memories_fts f "
                f"JOIN memories m ON m.id = f.rowid WHERE memories_fts MATCH ? {pin} "
                f"ORDER BY bm25(memories_fts) LIMIT ?", (match, limit)).fetchall()
        return self._rows(rows)

    def recent(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id, created, kind, text, source, pinned FROM memories ORDER BY pinned ASC, id DESC LIMIT ?",
                (limit,)).fetchall()
        return self._rows(rows)

    def count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    def context_for(self, query: str, pinned_limit: int = 25, related_limit: int = 8) -> str:
        """A compact block for prompts: who Patrick is, plus memories related to `query`."""
        parts = []
        pins = self.pinned(pinned_limit)
        if pins:
            parts.append("About Patrick:\n" + "\n".join(f"- {m['text']}" for m in pins))
        related = self.search(query, related_limit)
        if related:
            parts.append("Things you remember that may be relevant:\n" + "\n".join(f"- {m['text']}" for m in related))
        return "\n\n".join(parts)


# ---- Mission archive ----------------------------------------------------------

ARCHIVE_AFTER_DAYS = 365
REMIND_DAYS_BEFORE = 7


def archive_old_missions(missions_db: sqlite3.Connection, lock, now: datetime | None = None,
                         after_days: int = ARCHIVE_AFTER_DAYS) -> tuple[int, int]:
    """Move mission details older than a year into missions_archive. Returns (archived, due_next_week)."""
    now = now or _now()
    cutoff = (now - timedelta(days=after_days)).isoformat()
    soon = (now - timedelta(days=after_days - REMIND_DAYS_BEFORE)).isoformat()
    with lock:
        missions_db.execute("CREATE TABLE IF NOT EXISTS missions_archive AS SELECT * FROM missions WHERE 0")
        done = "status IN ('done', 'failed', 'cancelled')"
        missions_db.execute(f"INSERT INTO missions_archive SELECT * FROM missions WHERE {done} AND updated < ?", (cutoff,))
        cur = missions_db.execute(f"DELETE FROM missions WHERE {done} AND updated < ?", (cutoff,))
        archived = cur.rowcount
        due = missions_db.execute(f"SELECT COUNT(*) FROM missions WHERE {done} AND updated >= ? AND updated < ?",
                                  (cutoff, soon)).fetchone()[0]
        missions_db.commit()
    return archived, due
