"""Lead pipeline for the Revenue cell. Leads are found and drafted automatically, but every
contact with a business is Patrick's decision: Wetworks never sends anything itself."""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

STATUSES = ("new", "contacted", "replied", "won", "lost", "dismissed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _in_days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


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
        # Added later: follow-ups and pre-outreach research. ALTER keeps older databases working.
        have = {r[1] for r in self.db.execute("PRAGMA table_info(leads)")}
        for col, decl in self.EXTRA:
            if col not in have:
                self.db.execute(f"ALTER TABLE leads ADD COLUMN {col} {decl}")
        self.db.execute("CREATE INDEX IF NOT EXISTS leads_domain ON leads(domain)")
        self.db.commit()

    EXTRA = (("contacted_at", "TEXT NOT NULL DEFAULT ''"), ("followup_due", "TEXT NOT NULL DEFAULT ''"),
             ("followups", "INTEGER NOT NULL DEFAULT 0"), ("followup_draft", "TEXT NOT NULL DEFAULT ''"),
             ("research", "TEXT NOT NULL DEFAULT ''"), ("research_mission", "INTEGER NOT NULL DEFAULT 0"),
             ("review", "TEXT NOT NULL DEFAULT ''"))
    KEYS = ("id", "created", "updated", "business", "url", "domain", "location", "signal", "offer", "why",
            "fit", "status", "draft", "notes") + tuple(c for c, _ in EXTRA)

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

    def update(self, lid: int, status: str | None = None, notes: str | None = None, draft: str | None = None,
               followup_days: int | None = None) -> dict | None:
        """followup_days: when a lead first becomes 'contacted', when to suggest following up."""
        if status is not None and status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}")
        sets, vals = [], []
        old = self.get(lid) if status is not None else None
        if status == "contacted" and old and old["status"] != "contacted":
            sets += ["contacted_at = ?", "followup_due = ?", "followups = 0", "followup_draft = ''"]
            vals += [_now(), _in_days(followup_days) if followup_days else ""]
        elif status is not None and status != "contacted":
            sets += ["followup_due = ''", "followup_draft = ''"]  # they replied, or it's closed: stop nudging
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

    def due_count(self) -> int:
        with self.lock:
            return self.db.execute(
                "SELECT COUNT(*) FROM leads WHERE status = 'contacted' AND followup_due != '' AND followup_due <= ?",
                (_now(),)).fetchone()[0]

    # ---- follow-ups -----------------------------------------------------------------------
    def due_followups(self, limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM leads WHERE status = 'contacted' AND followup_due != '' AND followup_due <= ? "
                "ORDER BY followup_due LIMIT ?", (_now(), limit)).fetchall()
        return [self._row(r) for r in rows]

    def followed_up(self, lid: int, next_days: int | None) -> dict | None:
        """Patrick sent a follow-up. next_days=None means that was the last one."""
        with self.lock:
            self.db.execute("UPDATE leads SET followups = followups + 1, followup_draft = '', followup_due = ?, "
                            "updated = ? WHERE id = ? AND status = 'contacted'",
                            (_in_days(next_days) if next_days else "", _now(), lid))
            self.db.commit()
        return self.get(lid)

    def snooze(self, lid: int, days: int) -> dict | None:
        with self.lock:
            self.db.execute("UPDATE leads SET followup_due = ?, updated = ? WHERE id = ? AND status = 'contacted'",
                            (_in_days(days), _now(), lid))
            self.db.commit()
        return self.get(lid)

    def set_review(self, lid: int, review: dict) -> None:
        import json as _json

        with self.lock:
            self.db.execute("UPDATE leads SET review = ? WHERE id = ?", (_json.dumps(review), lid))
            self.db.commit()

    def set_followup_draft(self, lid: int, text: str) -> None:
        with self.lock:
            self.db.execute("UPDATE leads SET followup_draft = ?, updated = ? WHERE id = ?", (text[:4000], _now(), lid))
            self.db.commit()

    # ---- research before outreach ------------------------------------------------------------
    def needing_research(self, min_fit: int, limit: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM leads WHERE status = 'new' AND research_mission = 0 AND research = '' AND fit >= ? "
                "ORDER BY fit DESC, id DESC LIMIT ?", (min_fit, limit)).fetchall()
        return [self._row(r) for r in rows]

    def set_research(self, lid: int, mission: int | None = None, text: str | None = None) -> dict | None:
        sets, vals = [], []
        if mission is not None:
            sets.append("research_mission = ?"); vals.append(mission)
        if text is not None:
            sets.append("research = ?"); vals.append(text[:8000])
        if sets:
            with self.lock:
                self.db.execute(f"UPDATE leads SET {', '.join(sets)}, updated = ? WHERE id = ?", (*vals, _now(), lid))
                self.db.commit()
        return self.get(lid)

    def needing_drafts(self, min_fit: int, limit: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM leads WHERE status = 'new' AND draft = '' AND fit >= ? "
                "AND (research_mission = 0 OR research != '') "  # wait for pre-outreach research to land
                "ORDER BY fit DESC, id DESC LIMIT ?",
                (min_fit, limit)).fetchall()
        return [self._row(r) for r in rows]
