"""Base class every cell inherits from."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

from ..config import Settings
from ..events import EventLog
from ..findings import Finding, FindingsStore
from ..llm import LLM


@dataclass
class CellContext:
    settings: Settings
    llm: LLM
    findings: FindingsStore
    events: EventLog | None = None


class Cell(ABC):
    name: str = "cell"
    kind: str = "sleeper"  # "sleeper" (scheduled) or "splinter" (on-demand)
    trial_start: date | None = None  # new cells run a 2-week trial
    TRIAL_DAYS = 14

    def __init__(self, ctx: CellContext):
        self.ctx = ctx
        self.log = logging.getLogger(f"wetworks.{self.name}")

    def in_trial(self, today: date | None = None) -> bool:
        if self.trial_start is None:
            return False
        return ((today or date.today()) - self.trial_start).days < self.TRIAL_DAYS

    @abstractmethod
    def run(self) -> list[Finding]:
        """Do one pass of the cell's mission and return new findings (already stored)."""

    def safe_run(self) -> list[Finding]:
        """Run without ever crashing the scheduler. Logs to the activity feed when available."""
        ev = self.ctx.events
        if ev is not None:
            if self.name in ev.busy:
                self.log.info("%s already running; skipping", self.name)
                return []
            ev.busy.add(self.name)
            ev.add(self.name, "start", f"{self.name.title()} sweep started")
        try:
            found = self.run()
            self.log.info("%s run complete: %d new findings", self.name, len(found))
            if ev is not None:
                ev.add(self.name, "done", f"{self.name.title()} sweep done: {len(found)} new findings")
            return found
        except Exception as e:
            self.log.exception("%s run failed", self.name)
            if ev is not None:
                ev.add(self.name, "error", f"{self.name.title()} sweep failed: {type(e).__name__}")
            return []
        finally:
            if ev is not None:
                ev.busy.discard(self.name)
