"""Every agent decision, recorded. These are The Forge's training data: decisions made by the
stronger models (Gemini, Claude) teach the local model to drive Wetworks' agents."""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .runtime import parse_decision


class Traces:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, agent TEXT NOT NULL, brain TEXT NOT NULL,
                system TEXT NOT NULL, prompt TEXT NOT NULL, output TEXT NOT NULL, valid INTEGER NOT NULL)""")
        self.db.commit()

    def record(self, agent: str, brain: str, system: str, prompt: str, output: str) -> None:
        d = parse_decision(output)
        valid = int(bool(d) and ("done" in d or bool(d.get("tool"))))
        with self.lock:
            self.db.execute("INSERT INTO traces (ts, agent, brain, system, prompt, output, valid) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (datetime.now(timezone.utc).isoformat(), agent, brain, system, prompt, output, valid))
            self.db.commit()

    def counts(self) -> dict:
        with self.lock:
            rows = self.db.execute("SELECT brain, SUM(valid), COUNT(*) FROM traces GROUP BY brain").fetchall()
        return {b: {"valid": int(v or 0), "total": t} for b, v, t in rows}

    def examples(self, brains: tuple[str, ...], limit: int = 5000) -> list[dict]:
        q = ",".join("?" * len(brains))
        with self.lock:
            rows = self.db.execute(
                f"SELECT system, prompt, output FROM traces WHERE valid = 1 AND brain IN ({q}) ORDER BY id DESC LIMIT ?",
                (*brains, limit)).fetchall()
        return [{"system": s, "prompt": p, "output": o} for s, p, o in rows]


def recorded(ask, traces: Traces, agent: str, brain_of):
    """Wrap an ask() so each decision is stored. brain_of() says which brain just answered."""
    def wrapped(system: str, prompt: str, max_tokens: int) -> str:
        out = ask(system, prompt, max_tokens)
        try:
            traces.record(agent, brain_of(), system, prompt, out)
        except Exception:
            pass  # recording must never break an agent
        return out
    return wrapped
