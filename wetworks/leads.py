"""Lead pipeline for the Revenue cell. Leads are found and drafted automatically, but every
contact with a business is Patrick's decision: Wetworks never sends anything itself."""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

STATUSES = ("new", "contacted", "replied", "won", "lost", "dismissed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def domain_of(url: str) -> str:
    host = (urlsplit(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class LeadStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created TEXT NOT NULL,
                updated TEXT NOT NULL,
                business TEXT NOT NULL,
                url TEXT NOT NULL,
                domain TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '',
                signal TEXT NOT NULL,
                offer TEXT NOT NULL DEFAULT '',
                why TEXT NOT NULL DEFAULT '',
                fit INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                draft TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT ''
            )"""
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS leads_domain ON leads(domain)")
        self.db.commit()

    KEYS = ("id", "created", "updated", "business", "url", "domain", "location", "signal", "offer", "why",
            "fit", "status", "draft", "notes")

    def _row(self, r) -> dict:
        return dict(zip(self.KEYS, r))

    def known(self, url: str = "", business: str = "") -> bool:
        d = domain_of(url)
        with self.lock:
            if d:
                if self.db.execute("SELECT 1 FROM leads WHERE domain = ? LIMIT 1", (d,)).fetchone():
                    return True
            if business:
                return self.db.execute("SELECT 1 FROM leads WHERE lower(business) = lower(?) LIMIT 1",
                                       (business.strip(),)).fetchone() is not None
        return False

    def add(self, business: str, url: str, signal: str, fit: int, offer: str = "", why: str = "",
            location: str = "", own_site: bool = True) -> dict | None:
        """Store a new lead; returns None if the business is already in the pipeline.

        own_site=False means `url` is a job board or directory page, so it isn't used to
        recognise the business later (many businesses share the same job-board domain)."""
        business = business.strip()[:120]
        if not business or self.known(url if own_site else "", business):
            return None
        fit = max(1, min(5, int(fit)))
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO leads (created, updated, business, url, domain, location, signal, offer, why, fit) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_now(), _now(), business, url.strip(), domain_of(url) if own_site else "", location.strip()[:80],
                 signal.strip()[:400], offer.strip()[:80], why.strip()[:400], fit),
            )
            self.db.commit()
            return self.get(cur.lastrowid, locked=True)

    def get(self, lid: int, locked: bool = False) -> dict | None:
        q = "SELECT * FROM leads WHERE id = ?"
        if locked:
            r = self.db.execute(q, (lid,)).fetchone()
        else:
            with self.lock:
                r = self.db.execute(q, (lid,)).fetchone()
        return self._row(r) if r else None

    def recent(self, status: str | None = None, limit: int = 100) -> list[dict]:
        with self.lock:
            if status:
                rows = self.db.execute("SELECT * FROM leads WHERE status = ? ORDER BY fit DESC, id DESC LIMIT ?",
                                       (status, limit)).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM leads WHERE status != 'dismissed' ORDER BY "
                    "CASE status WHEN 'new' THEN 0 WHEN 'replied' THEN 1 WHEN 'contacted' THEN 2 ELSE 3 END, "
                    "fit DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def update(self, lid: int, status: str | None = None, notes: str | None = None, draft: str | None = None) -> dict | None:
        if status is not None and status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}")
        sets, vals = [], []
        for col, val in (("status", status), ("notes", notes), ("draft", draft)):
            if val is not None:
                sets.append(f"{col} = ?")
                vals.append(val[:4000] if isinstance(val, str) else val)
        if not sets:
            return self.get(lid)
        with self.lock:
            self.db.execute(f"UPDATE leads SET {', '.join(sets)}, updated = ? WHERE id = ?", (*vals, _now(), lid))
            self.db.commit()
        return self.get(lid)

    def counts(self) -> dict:
        with self.lock:
            return dict(self.db.execute("SELECT status, COUNT(*) FROM leads GROUP BY status").fetchall())

    def needing_drafts(self, min_fit: int, limit: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM leads WHERE status = 'new' AND draft = '' AND fit >= ? ORDER BY fit DESC, id DESC LIMIT ?",
                (min_fit, limit)).fetchall()
        return [self._row(r) for r in rows]
