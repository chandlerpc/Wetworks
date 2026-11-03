"""Spend ledger: every model call is recorded with tokens and cost."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# USD per million tokens (input, output). Update when vendors change prices.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

# USD per web search call.
SEARCH_PRICE = {"claude": 0.01, "grok": 0.01}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    if model not in PRICES:
        raise KeyError(f"No price for model {model!r}; add it to ledger.PRICES")
    pin, pout = PRICES[model]
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS calls (
                ts TEXT NOT NULL,
                agent TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL
            )"""
        )
        self.db.commit()

    def record(
        self, agent: str, model: str, input_tokens: int, output_tokens: int, extra_usd: float = 0.0
    ) -> float:
        """Record a call. extra_usd covers non-token charges such as web searches."""
        cost = cost_usd(model, input_tokens, output_tokens) + extra_usd
        return self.record_cost(agent, model, input_tokens, output_tokens, cost)

    def record_cost(self, agent: str, model: str, input_tokens: int, output_tokens: int, cost: float) -> float:
        """Record a call whose cost was computed elsewhere (e.g. another vendor's pricing)."""
        self.db.execute(
            "INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), agent, model, input_tokens, output_tokens, cost),
        )
        self.db.commit()
        return cost

    def month_to_date(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM calls WHERE ts >= ?", (month_start.isoformat(),)
        ).fetchone()
        return float(row[0])

    def since(self, start: datetime) -> list[tuple[str, float]]:
        """Spend per agent since `start`."""
        return [
            (agent, float(total))
            for agent, total in self.db.execute(
                "SELECT agent, SUM(cost_usd) FROM calls WHERE ts >= ? GROUP BY agent ORDER BY 2 DESC",
                (start.isoformat(),),
            )
        ]
