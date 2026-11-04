"""The Architect — overseer. Compiles the daily briefs from host status and cell findings."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Settings
from .events import EventLog
from .findings import Finding, FindingsStore
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

MAX_BRIEF_FINDINGS = 12


def format_findings(findings: list[Finding]) -> str:
    if not findings:
        return ""
    by_topic: dict[str, list[Finding]] = {}
    for f in findings[:MAX_BRIEF_FINDINGS]:
        by_topic.setdefault(f.topic, []).append(f)
    lines = []
    for topic, items in by_topic.items():
        lines.append(f"__{topic}__")
        for f in items:
            flag = "🔴 " if f.importance >= 5 else "🟠 " if f.importance == 4 else ""
            link = f" <{f.url}>" if f.url else ""
            lines.append(f"• {flag}**{f.title}** — {f.summary}{link}")
    return "\n".join(lines)


class Architect:
    def __init__(self, settings: Settings):
        self.s = settings
        self.ledger = Ledger(settings.data_dir / "ledger.db")
        self.findings = FindingsStore(settings.data_dir / "findings.db")
        self.events = EventLog(settings.data_dir / "events.db")
        self.llm = LLM(settings, self.ledger)
        self.notify = Notifier(settings.discord_webhook_url, dry_run=settings.dry_run)
        self.tz = ZoneInfo(settings.timezone)
        self.min_importance = 2

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

    def context_block(self, limit: int = 8) -> str:
        """Status plus the latest findings, for comms replies."""
        rows = self.findings.db.execute(
            "SELECT topic, title, summary, importance FROM findings ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        recent = "\n".join(f"- [{t}] (imp {i}) {ti}: {s}" for t, ti, s, i in rows) or "(none yet)"
        busy = ", ".join(sorted(self.events.busy)) or "nobody"
        leads = getattr(self, "leads", None)
        if leads is not None:
            c = leads.counts()
            recent = recent + (f"\nLead pipeline: {c.get('new', 0)} new, {c.get('contacted', 0)} contacted, "
                               f"{c.get('replied', 0)} replied, {c.get('won', 0)} won.")
        mtd, budget = self.ledger.month_to_date(), self.s.monthly_budget_usd
        mode = "free local model" if self.s.provider == "local" else f"paid {self.s.provider}"
        facts = (
            f"Money spent on paid AI this month: ${mtd:.2f} out of a ${budget:.2f} budget.\n"
            f"Wetworks is currently running on: {mode} (new work costs $0 while on the free model; "
            f"the ${mtd:.2f} was spent earlier)."
        )
        return f"{facts}\n{self.status_block()}\nWorking right now: {busy}\nLatest Intel findings:\n{recent}"

    def brief(self, kind: str) -> str:
        """kind = morning | evening. Builds the brief, sends it, and marks findings reported."""
        self.events.busy.add("architect")
        self.events.add("architect", "start", f"Compiling the {kind} brief")
        try:
            text = self._brief(kind)
            self.events.add("architect", "done", f"{kind.title()} brief sent to Discord")
            return text
        except Exception as e:
            self.events.add("architect", "error", f"{kind.title()} brief failed: {type(e).__name__}")
            raise
        finally:
            self.events.busy.discard("architect")

    def _brief(self, kind: str) -> str:
        stamp = datetime.now(self.tz).strftime("%a %b %d, %I:%M %p %Z")
        status = self.status_block()
        findings = self.findings.unreported(self.min_importance)
        intel = format_findings(findings)

        digest = "\n".join(f"- [{f.topic}] (imp {f.importance}) {f.title}: {f.summary}" for f in findings[:MAX_BRIEF_FINDINGS])
        prompt = (
            f"Write the {kind} brief. Status data:\n{status}\n\n"
            f"New Intel findings:\n{digest or '(none this cycle)'}\n\n"
            "Give a 2-4 line summary of what matters most"
            + (" and one suggested focus for today drawn from the mission priorities" if kind == "morning" else "")
            + ". Don't repeat every finding; they're listed below your summary. Under 120 words."
        )
        try:
            if self.s.provider == "local":
                reply = self.llm.ask_local("architect", prompt, system=SYSTEM, max_tokens=400)
            else:
                reply = self.llm.ask("architect", prompt, tier="work", system=SYSTEM, max_tokens=400, essential=True)
            body = reply.text.strip()
        except BudgetExceeded:
            body = "Budget exhausted; summary skipped."
        except Exception as e:  # never let a model error kill the brief
            body = f"Model call failed ({type(e).__name__}); raw status only."

        parts = [f"**{kind.title()} brief — {stamp}**", body]
        if intel:
            parts += ["", "**Intel**", intel]
        parts += ["", f"```\n{status}\n```"]
        text = "\n".join(parts)
        self.notify.send(text)
        self.findings.mark_reported()
        return text
