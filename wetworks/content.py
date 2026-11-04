"""Content: ideas, drafts and a posting calendar. Wetworks never posts; Patrick copies and posts himself."""

import json
import re
import sqlite3
import threading
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

STATUSES = ("idea", "draft", "scheduled", "posted", "dropped")
CHANNELS = {"linkedin": "LinkedIn", "x": "X", "blog": "Blog", "portfolio": "Portfolio update"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Content:
    def __init__(self, path: Path, config_path: Path | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.config_path = config_path
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS pieces (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, updated TEXT NOT NULL,
                title TEXT NOT NULL, angle TEXT NOT NULL, source TEXT NOT NULL, pillar TEXT NOT NULL, status TEXT NOT NULL,
                drafts TEXT NOT NULL DEFAULT '{}', planned TEXT NOT NULL DEFAULT '', channel TEXT NOT NULL DEFAULT '',
                posted_url TEXT NOT NULL DEFAULT '', flags TEXT NOT NULL DEFAULT '');
        """)
        self.db.commit()

    def config(self) -> dict:
        if not self.config_path or not self.config_path.exists():
            return {}
        with open(self.config_path, "rb") as f:
            return tomllib.load(f).get("content", {})

    def channels(self) -> list[str]:
        return [c for c in self.config().get("channels", list(CHANNELS)) if c in CHANNELS]

    KEYS = ("id", "created", "updated", "title", "angle", "source", "pillar", "status", "drafts", "planned", "channel",
            "posted_url", "flags")

    def _row(self, r) -> dict:
        d = dict(zip(self.KEYS, r))
        d["drafts"] = json.loads(d["drafts"] or "{}")
        return d

    def get(self, pid: int) -> dict | None:
        r = self.db.execute(f"SELECT {', '.join(self.KEYS)} FROM pieces WHERE id = ?", (pid,)).fetchone()
        return self._row(r) if r else None

    def pieces(self, status: str = "", limit: int = 100) -> list[dict]:
        if status:
            rows = self.db.execute(f"SELECT {', '.join(self.KEYS)} FROM pieces WHERE status = ? ORDER BY id DESC LIMIT ?",
                                   (status, limit)).fetchall()
        else:
            rows = self.db.execute(f"SELECT {', '.join(self.KEYS)} FROM pieces WHERE status != 'dropped' ORDER BY "
                                   "CASE status WHEN 'draft' THEN 0 WHEN 'scheduled' THEN 1 WHEN 'idea' THEN 2 ELSE 3 END, "
                                   "planned, id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def known(self, title: str) -> bool:
        key = re.sub(r"[^a-z0-9 ]", "", title.lower())[:60]
        rows = self.db.execute("SELECT title FROM pieces").fetchall()
        return any(re.sub(r"[^a-z0-9 ]", "", t.lower())[:60] == key for (t,) in rows)

    def add_idea(self, title: str, angle: str = "", source: str = "patrick", pillar: str = "") -> dict | None:
        title = title.strip()[:160]
        if not title or self.known(title):
            return None
        with self.lock:
            cur = self.db.execute("INSERT INTO pieces (created, updated, title, angle, source, pillar, status) VALUES (?, ?, ?, ?, ?, ?, 'idea')",
                                  (_now(), _now(), title, angle.strip()[:1000], source[:80], pillar[:200]))
            self.db.commit()
        return self.get(cur.lastrowid)

    def flagged(self, text: str) -> list[str]:
        low = (text or "").lower()
        return [w for w in self.config().get("never_mention", []) if w.lower() in low]

    def set_draft(self, pid: int, channel: str, text: str) -> dict | None:
        p = self.get(pid)
        if not p or channel not in CHANNELS:
            return None
        drafts = p["drafts"]; drafts[channel] = text.strip()[:12000]
        flags = sorted({f for t in drafts.values() for f in self.flagged(t)})
        with self.lock:
            self.db.execute("UPDATE pieces SET drafts = ?, flags = ?, status = CASE WHEN status = 'idea' THEN 'draft' ELSE status END, "
                            "updated = ? WHERE id = ?", (json.dumps(drafts), ", ".join(flags), _now(), pid))
            self.db.commit()
        return self.get(pid)

    def update(self, pid: int, status: str | None = None, planned: str | None = None, channel: str | None = None,
               posted_url: str | None = None) -> dict | None:
        if status is not None and status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}")
        if planned:
            date.fromisoformat(planned)
        sets, vals = [], []
        for col, v in (("status", status), ("planned", planned), ("channel", channel), ("posted_url", posted_url)):
            if v is not None:
                sets.append(f"{col} = ?"); vals.append(v)
        if planned and status is None:
            sets.append("status = 'scheduled'")
        if sets:
            with self.lock:
                self.db.execute(f"UPDATE pieces SET {', '.join(sets)}, updated = ? WHERE id = ?", (*vals, _now(), pid))
                self.db.commit()
        return self.get(pid)

    def calendar(self, days: int = 21, today: date | None = None) -> list[dict]:
        """Planned posts plus open slots from the cadence, so gaps are visible."""
        today = today or date.today()
        planned = [p for p in self.pieces() if p["planned"] and today.isoformat() <= p["planned"] <= (today + timedelta(days=days)).isoformat()]
        slots = []
        for ch, per_week in (self.config().get("cadence") or {}).items():
            if ch not in self.channels() or not per_week:
                continue
            step = max(1, round(7 / float(per_week)))
            for k in range(0, days, step):
                d = (today + timedelta(days=k + 1)).isoformat()
                if not any(p["planned"] == d and p["channel"] == ch for p in planned):
                    slots.append({"date": d, "channel": ch})
        return sorted([{"date": p["planned"], "channel": p["channel"], "piece": p["id"], "title": p["title"], "status": p["status"]}
                       for p in planned] + slots, key=lambda x: (x["date"], x["channel"]))

    def counts(self) -> dict:
        return dict(self.db.execute("SELECT status, COUNT(*) FROM pieces GROUP BY status").fetchall())
