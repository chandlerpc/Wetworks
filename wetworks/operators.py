"""Operators manage cells. The Sleeper Operator schedules background cells."""

import logging
import threading
from datetime import datetime, timedelta

from apscheduler.triggers.cron import CronTrigger

from .cells.base import Cell

log = logging.getLogger("wetworks.operators")


def offset_hhmm(hhmm: str, minutes_before: int) -> tuple[int, int]:
    """Return (hour, minute) `minutes_before` earlier than HH:MM, wrapping past midnight."""
    h, m = map(int, hhmm.split(":"))
    t = datetime(2000, 1, 1, h, m) - timedelta(minutes=minutes_before)
    return t.hour, t.minute


class SleeperOperator:
    """Wakes Sleeper cells on schedules. Milestone 2: cells run shortly before each brief."""

    def __init__(self, scheduler, timezone: str):
        self.sched = scheduler
        self.tz = timezone
        self.cells: dict[str, Cell] = {}

    def register(self, cell: Cell, times: list[str], lead_minutes: int = 0) -> None:
        self.cells[cell.name] = cell
        for hhmm in times:
            h, m = offset_hhmm(hhmm, lead_minutes)
            self.sched.add_job(
                cell.safe_run,
                CronTrigger(hour=h, minute=m, timezone=self.tz),
                id=f"{cell.name}_{h:02d}{m:02d}",
                misfire_grace_time=1800,
                coalesce=True,
                max_instances=1,
            )
            log.info("Sleeper %s scheduled at %02d:%02d %s", cell.name, h, m, self.tz)


class SplinterOperator:
    """Runs missions from the queue, one at a time (the GPU runs one model job well at a time)."""

    def __init__(self, store, splinter, events=None, notify=None, on_done=None):
        self.store = store
        self.splinter = splinter
        self.events = events
        self.notify = notify
        self.on_done = on_done  # callback(mission) e.g. post into the comms channel that asked
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        recovered = self.store.recover()
        if recovered and self.events:
            self.events.add("splinter", "info", f"Resumed {recovered} mission(s) interrupted by a restart")
        self.thread = threading.Thread(target=self._loop, name="splinter-operator", daemon=True)
        self.thread.start()
        self.wake.set()

    def submit(self) -> None:
        """Call after queueing a mission so the worker picks it up right away."""
        self.wake.set()

    def _loop(self) -> None:
        while True:
            self.wake.wait(timeout=60)
            self.wake.clear()
            while (m := self.store.next_queued()) is not None:
                self.run_one(m)

    def run_one(self, m: dict) -> dict:
        from .cells.splinter import Cancelled

        mid = m["id"]
        self.store.set_status(mid, "running")
        if self.events:
            self.events.busy.add("splinter")
            self.events.add("splinter", "start", f"Mission #{mid} started: {m['title']}")
        try:
            result = self.splinter.run(m)
            self.store.set_status(mid, "done", result=result.text, sources=result.sources)
            if self.events:
                self.events.add("splinter", "done", f"Mission #{mid} done: {m['title']}")
            if self.notify:
                try:
                    self.notify.send(f"✅ Mission #{mid} done: **{m['title']}**. Open Missions in the console to read it.")
                except Exception:
                    log.exception("Couldn't post mission #%d to Discord", mid)
        except Cancelled:
            self.store.set_status(mid, "cancelled")
            if self.events:
                self.events.add("splinter", "warn", f"Mission #{mid} cancelled")
        except Exception as e:
            log.exception("Mission #%d failed", mid)
            self.store.set_status(mid, "failed", result=f"This mission failed: {type(e).__name__}: {e}")
            if self.events:
                self.events.add("splinter", "error", f"Mission #{mid} failed: {type(e).__name__}")
        finally:
            self.store.cancel_requested.discard(mid)
            if self.events:
                self.events.busy.discard("splinter")
        final = self.store.get(mid)
        if self.on_done:
            try:
                self.on_done(final)
            except Exception:
                log.exception("on_done callback failed for mission #%d", mid)
        return final
