"""Operators manage cells. The Sleeper Operator schedules background cells."""

import logging
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
