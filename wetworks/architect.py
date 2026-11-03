"""The Architect — overseer. Milestone 1: compiles and sends the daily briefs."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Settings
from .health import format_health, snapshot
from .ledger import Ledger
from .llm import LLM, BudgetExceeded
from .notify import Notifier

SYSTEM = (
    "You are The Architect, overseer of Wetworks, Patrick's multi-agent ops center. "
    "Write terse, useful briefs in the voice of a calm ops-center handler. "
    "No filler, no invented facts: report only what's in the data you're given. "
    "Mission priorities: make money, make the day job easier, manage finances, "
    "free up time, build games, create content."
)


class Architect:
    def __init__(self, settings: Settings):
        self.s = settings
        self.ledger = Ledger(settings.data_dir / "ledger.db")
        self.llm = LLM(settings, self.ledger)
        self.notify = Notifier(settings.discord_webhook_url, dry_run=settings.dry_run)
        self.tz = ZoneInfo(settings.timezone)

    def status_block(self) -> str:
        now = datetime.now(timezone.utc)
        mtd = self.ledger.month_to_date(now)
        budget = self.s.monthly_budget_usd
        lines = [
            f"Spend MTD: ${mtd:.2f} / ${budget:.0f} ({mtd / budget:.0%})",
            f"Host: {format_health(snapshot(self.s.data_dir))}",
        ]
        by_agent = self.ledger.since(now - timedelta(hours=24))
        if by_agent:
            lines.append("Last 24h: " + ", ".join(f"{a} ${c:.2f}" for a, c in by_agent))
        if mtd >= budget * self.s.budget_warn_ratio:
            lines.append(f"⚠ Budget at {mtd / budget:.0%}. Non-essential calls stop at 100%.")
        return "\n".join(lines)

    def brief(self, kind: str) -> str:
        """kind = morning | evening. Builds the brief and sends it."""
        stamp = datetime.now(self.tz).strftime("%a %b %d, %I:%M %p %Z")
        status = self.status_block()
        title = f"**{kind.title()} brief — {stamp}**"
        prompt = (
            f"Write the {kind} brief. Status data:\n{status}\n\n"
            "No cells are active yet (Revenue, Code, Intel, The Forge are planned). "
            "Give a 2-4 line summary and, for the morning, one suggested focus for today "
            "drawn from the mission priorities. Under 120 words."
        )
        try:
            reply = self.llm.ask("architect", prompt, tier="work", system=SYSTEM, max_tokens=400, essential=True)
            body = reply.text.strip()
        except BudgetExceeded:
            body = "Budget exhausted; summary skipped."
        except Exception as e:  # never let a model error kill the brief
            body = f"Model call failed ({type(e).__name__}); raw status only."
        text = f"{title}\n{body}\n\n```\n{status}\n```"
        self.notify.send(text)
        return text
