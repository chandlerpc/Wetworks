"""Base class every cell inherits from."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

from ..config import Settings
from ..findings import Finding, FindingsStore
from ..llm import LLM


@dataclass
class CellContext:
    settings: Settings
    llm: LLM
    findings: FindingsStore


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
        """Run without ever crashing the scheduler."""
        try:
            found = self.run()
            self.log.info("%s run complete: %d new findings", self.name, len(found))
            return found
        except Exception:
            self.log.exception("%s run failed", self.name)
            return []
