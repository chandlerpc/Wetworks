"""The Observer: learns how Patrick's day works, on his own machines, and suggests how Wetworks can fit it.

A small helper (observer/agent.py) on the Mac or the MSI reports the foreground app and window title
every few seconds, plus an occasional screenshot. Screenshots are described in one sentence by the local
model and then thrown away. Excluded apps and titles (work, money, passwords, private browsing) are never
recorded beyond "Private" time. Nothing here is sent to a cloud model.
"""

import json
import re
import sqlite3
import statistics
import threading
import tomllib
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SHOT_PROMPT = ("In one sentence, say what the person is doing on this screen (the task, not the pixels). Name the app "
               "and the project or topic if visible. Do not copy any personal details, names, numbers, emails, "
               "messages or passwords.")
PRIVATE = "Private"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Observer:
    def __init__(self, path: Path, config_path: Path | None = None, tz: str = "America/New_York"):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.config_path = config_path
        self.tz = ZoneInfo(tz)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, device TEXT NOT NULL,
                app TEXT NOT NULL, title TEXT NOT NULL, secs INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
            CREATE TABLE IF NOT EXISTS shots (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, device TEXT NOT NULL,
                app TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, recording INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS recordings (id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT NOT NULL, ended TEXT,
                device TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL, playbook TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS devices (name TEXT PRIMARY KEY, last_seen TEXT NOT NULL, platform TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS digests (day TEXT PRIMARY KEY, text TEXT NOT NULL, stats TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS suggested (key TEXT PRIMARY KEY, ts TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
        """)
        self.db.commit()

    # ---- settings -----------------------------------------------------------------------
    def config(self) -> dict:
        if not self.config_path or not self.config_path.exists():
            return {}
        with open(self.config_path, "rb") as f:
            return tomllib.load(f).get("observer", {})

    def paused(self) -> bool:
        r = self.db.execute("SELECT v FROM kv WHERE k = 'paused'").fetchone()
        return bool(r and r[0] == "1")

    def set_paused(self, on: bool) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES ('paused', ?)", ("1" if on else "0",))
            self.db.commit()

    def active_recording(self, device: str) -> dict | None:
        r = self.db.execute("SELECT id, name FROM recordings WHERE device = ? AND status = 'recording' ORDER BY id DESC LIMIT 1",
                            (device,)).fetchone()
        return {"id": r[0], "name": r[1]} if r else None

    def helper_config(self, device: str) -> dict:
        """What the helper on a machine should do right now."""
        c = self.config()
        rec = self.active_recording(device)
        return {"enabled": bool(c.get("enabled", True)) and not self.paused(), "paused": self.paused(),
                "screenshots": bool(c.get("screenshots", True)), "shot_every": int(c.get("screenshot_every_seconds", 60)),
                "idle_after": int(c.get("idle_after_seconds", 300)), "exclude_apps": c.get("exclude_apps", []),
                "exclude_title_words": c.get("exclude_title_words", []), "recording": rec,
                "record_every": 5}

    def excluded(self, app: str, title: str) -> bool:
        c = self.config()
        a, t = (app or "").lower(), (title or "").lower()
        if any(x.lower() == a or x.lower() in a for x in c.get("exclude_apps", [])):
            return True
        return any(w.lower() in t or w.lower() in a for w in c.get("exclude_title_words", []))

    # ---- intake -----------------------------------------------------------------------------
    def add_samples(self, device: str, platform: str, samples: list[dict]) -> int:
        device = re.sub(r"[^\w .-]", "", device)[:40] or "device"
        rows = []
        for s in samples[:2000]:
            try:
                ts = datetime.fromisoformat(str(s["ts"]).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
            except (KeyError, ValueError):
                continue
            app, title = str(s.get("app", ""))[:80], str(s.get("title", ""))[:200]
            if self.excluded(app, title):
                app, title = PRIVATE, ""
            rows.append((ts, device, app or "Unknown", title, max(1, min(600, int(s.get("secs", 5))))))
        with self.lock:
            self.db.executemany("INSERT INTO samples (ts, device, app, title, secs) VALUES (?, ?, ?, ?, ?)", rows)
            self.db.execute("INSERT OR REPLACE INTO devices (name, last_seen, platform) VALUES (?, ?, ?)",
                            (device, _now().isoformat(), platform[:20]))
            self.db.commit()
        return len(rows)

    def add_shot(self, device: str, app: str, title: str, description: str, recording: int = 0) -> int | None:
        if self.excluded(app, title) or not description:
            return None
        from .dayjob import scrub  # belt and braces: strip anything that looks like a personal detail

        description = scrub(description)[0][:400]
        with self.lock:
            cur = self.db.execute("INSERT INTO shots (ts, device, app, title, description, recording) VALUES (?, ?, ?, ?, ?, ?)",
                                  (_now().isoformat(), device[:40], app[:80], title[:200], description, recording))
            self.db.commit()
        return cur.lastrowid

    # ---- teach a task -------------------------------------------------------------------------
    def start_recording(self, device: str, name: str) -> dict:
        with self.lock:
            self.db.execute("UPDATE recordings SET status = 'stopped', ended = ? WHERE device = ? AND status = 'recording'",
                            (_now().isoformat(), device))
            cur = self.db.execute("INSERT INTO recordings (started, device, name, status) VALUES (?, ?, ?, 'recording')",
                                  (_now().isoformat(), device, name.strip()[:80] or "Untitled task"))
            self.db.commit()
        return self.recording(cur.lastrowid)

    def stop_recording(self, rid: int) -> dict | None:
        with self.lock:
            self.db.execute("UPDATE recordings SET status = 'stopped', ended = ? WHERE id = ? AND status = 'recording'",
                            (_now().isoformat(), rid))
            self.db.commit()
        return self.recording(rid)

    def set_playbook(self, rid: int, text: str) -> None:
        with self.lock:
            self.db.execute("UPDATE recordings SET playbook = ?, status = 'ready' WHERE id = ?", (text, rid))
            self.db.commit()

    def recording(self, rid: int) -> dict | None:
        r = self.db.execute("SELECT id, started, ended, device, name, status, playbook FROM recordings WHERE id = ?", (rid,)).fetchone()
        if not r:
            return None
        d = dict(zip(("id", "started", "ended", "device", "name", "status", "playbook"), r))
        d["steps"] = [dict(zip(("ts", "app", "title", "description"), x)) for x in self.db.execute(
            "SELECT ts, app, title, description FROM shots WHERE recording = ? ORDER BY id", (rid,)).fetchall()]
        return d

    def recordings(self, limit: int = 20) -> list[dict]:
        ids = [r[0] for r in self.db.execute("SELECT id FROM recordings ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
        return [self.recording(i) for i in ids]

    # ---- reading ------------------------------------------------------------------------------
    def _local_day_bounds(self, day: date) -> tuple[str, str]:
        start = datetime(day.year, day.month, day.day, tzinfo=self.tz)
        return start.astimezone(timezone.utc).isoformat(), (start + timedelta(days=1)).astimezone(timezone.utc).isoformat()

    def day(self, day: date | None = None) -> dict:
        """One day's activity: active time, apps, focus blocks, switches, an hourly strip, what was on screen."""
        day = day or datetime.now(self.tz).date()
        a, b = self._local_day_bounds(day)
        with self.lock:
            rows = self.db.execute("SELECT ts, device, app, title, secs FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts", (a, b)).fetchall()
            shots = self.db.execute("SELECT ts, device, app, description FROM shots WHERE ts >= ? AND ts < ? AND recording = 0 "
                                    "ORDER BY ts", (a, b)).fetchall()
        apps = Counter(); titles = Counter(); hours = defaultdict(Counter); devices = Counter()
        blocks, switches, cur, cur_start, cur_secs, last_ts = [], 0, None, None, 0, None
        for ts, dev, app, title, secs in rows:
            t = datetime.fromisoformat(ts).astimezone(self.tz)
            apps[app] += secs; devices[dev] += secs; hours[t.hour][app] += secs
            if title and app != PRIVATE:
                titles[f"{app} — {title}"] += secs
            gap = last_ts and (t - last_ts).total_seconds() > 300
            if app != cur or gap:
                if cur is not None and cur_secs >= 25 * 60:
                    blocks.append({"app": cur, "start": cur_start.strftime("%H:%M"), "minutes": round(cur_secs / 60)})
                if cur is not None and not gap:
                    switches += 1
                cur, cur_start, cur_secs = app, t, 0
            cur_secs += secs; last_ts = t
        if cur is not None and cur_secs >= 25 * 60:
            blocks.append({"app": cur, "start": cur_start.strftime("%H:%M"), "minutes": round(cur_secs / 60)})
        first = datetime.fromisoformat(rows[0][0]).astimezone(self.tz).strftime("%H:%M") if rows else None
        last = datetime.fromisoformat(rows[-1][0]).astimezone(self.tz).strftime("%H:%M") if rows else None
        return {"day": day.isoformat(), "active_minutes": round(sum(apps.values()) / 60), "first": first, "last": last,
                "apps": [{"app": k, "minutes": round(v / 60)} for k, v in apps.most_common(12) if v >= 60],
                "top_windows": [{"window": k, "minutes": round(v / 60)} for k, v in titles.most_common(10) if v >= 120],
                "devices": {k: round(v / 60) for k, v in devices.items()},
                "focus_blocks": blocks, "switches": switches,
                "hours": {h: c.most_common(1)[0][0] for h, c in sorted(hours.items())},
                "screens": [{"time": datetime.fromisoformat(t).astimezone(self.tz).strftime("%H:%M"), "device": d, "app": ap,
                             "what": desc} for t, d, ap, desc in shots][-60:]}

    def devices(self) -> list[dict]:
        rows = self.db.execute("SELECT name, last_seen, platform FROM devices ORDER BY last_seen DESC").fetchall()
        out = []
        for n, seen, plat in rows:
            live = (_now() - datetime.fromisoformat(seen)).total_seconds() < 180
            out.append({"name": n, "last_seen": seen, "platform": plat, "live": live})
        return out

    def patterns(self, days: int = 14, today: date | None = None) -> dict:
        """Habits across recent days: when the day starts and ends, and how it usually opens."""
        today = today or datetime.now(self.tz).date()
        starts, ends, openers, app_days = [], [], Counter(), Counter()
        active_days = 0
        for i in range(1, days + 1):
            d = today - timedelta(days=i)
            a, b = self._local_day_bounds(d)
            rows = self.db.execute("SELECT ts, app FROM samples WHERE ts >= ? AND ts < ? AND app != ? ORDER BY ts", (a, b, PRIVATE)).fetchall()
            if len(rows) < 60:
                continue
            active_days += 1
            t0 = datetime.fromisoformat(rows[0][0]).astimezone(self.tz)
            t1 = datetime.fromisoformat(rows[-1][0]).astimezone(self.tz)
            starts.append(t0.hour * 60 + t0.minute); ends.append(t1.hour * 60 + t1.minute)
            first_apps = []
            for ts, app in rows:
                if (datetime.fromisoformat(ts).astimezone(self.tz) - t0).total_seconds() > 1800:
                    break
                if app not in first_apps:
                    first_apps.append(app)
            openers[tuple(first_apps[:3])] += 1
            for app in {r[1] for r in rows}:
                app_days[app] += 1
        hm = lambda m: f"{int(m) // 60:02d}:{int(m) % 60:02d}"
        opener, n = (openers.most_common(1)[0] if openers else ((), 0))
        return {"days_seen": active_days, "usual_start": hm(statistics.median(starts)) if starts else None,
                "usual_end": hm(statistics.median(ends)) if ends else None,
                "morning_routine": list(opener) if n >= 3 else [], "routine_days": n,
                "daily_apps": [a for a, c in app_days.most_common(8) if active_days and c >= max(3, active_days * 0.6)]}

    # ---- digests and suggestions ------------------------------------------------------------------
    def save_digest(self, day: str, text: str, stats: dict) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO digests (day, text, stats) VALUES (?, ?, ?)", (day, text, json.dumps(stats)))
            self.db.commit()

    def digests(self, limit: int = 7) -> list[dict]:
        rows = self.db.execute("SELECT day, text, stats FROM digests ORDER BY day DESC LIMIT ?", (limit,)).fetchall()
        return [{"day": d, "text": t, "stats": json.loads(s)} for d, t, s in rows]

    def mark_suggested(self, key: str) -> bool:
        """True the first time a suggestion key is seen (so declined ideas aren't asked again)."""
        with self.lock:
            try:
                self.db.execute("INSERT INTO suggested (key, ts) VALUES (?, ?)", (key[:200], _now().isoformat()))
                self.db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def prune(self) -> int:
        keep = int(self.config().get("keep_days", 14))
        cutoff = (_now() - timedelta(days=keep)).isoformat()
        with self.lock:
            n = self.db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
            n += self.db.execute("DELETE FROM shots WHERE ts < ? AND recording = 0", (cutoff,)).rowcount
            self.db.commit()
        return n

    def forget(self, what: str = "today") -> int:
        """Delete raw activity: today | all."""
        with self.lock:
            if what == "all":
                n = self.db.execute("DELETE FROM samples").rowcount + self.db.execute("DELETE FROM shots WHERE recording = 0").rowcount
                self.db.execute("DELETE FROM digests")
            else:
                a, _ = self._local_day_bounds(datetime.now(self.tz).date())
                n = self.db.execute("DELETE FROM samples WHERE ts >= ?", (a,)).rowcount
                n += self.db.execute("DELETE FROM shots WHERE ts >= ? AND recording = 0", (a,)).rowcount
            self.db.commit()
        return n
