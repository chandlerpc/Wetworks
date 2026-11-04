"""The kill switch. When halted, nothing in Wetworks starts new work on its own: no Architect
cycles, no Sleeper runs, no new missions, no training. Chat still answers. Survives restarts."""

import threading
from pathlib import Path


class Halt:
    def __init__(self, path: Path):
        self.path = path
        self.event = threading.Event()
        if path.exists():
            self.event.set()

    def is_set(self) -> bool:
        return self.event.is_set()

    def set(self, on: bool) -> None:
        if on:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("halted")
            self.event.set()
        else:
            self.path.unlink(missing_ok=True)
            self.event.clear()
