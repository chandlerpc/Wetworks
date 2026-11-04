"""Finance cell (Sleeper). Runs before the morning brief:

1. Sorts charges no rule matched, using the local model only (merchant names never leave the MSI).
2. Spots bills due soon, categories close to budget, and new recurring charges.
3. Reports them to the brief at the level Patrick allows (config/finance.toml `brief`).
"""

import json
from datetime import date, datetime, timedelta, timezone

from ..finance import CATEGORIES, Finance
from ..findings import Finding
from .base import Cell

SORT_PROMPT = """Put each merchant from a personal bank statement into one category.
Categories: {cats}
Merchants:
{merchants}
Reply with ONLY a JSON object mapping each merchant exactly as written to a category, e.g.
{{"publix super": "Groceries"}}. Use "Other" when unsure."""


class FinanceCell(Cell):
    name = "finance"
    kind = "sleeper"

    def __init__(self, ctx, finance: Finance):
        super().__init__(ctx)
        self.finance = finance

    def run(self) -> list:
        cfg = self.finance.config()
        if not cfg.get("enabled", True) or not self.finance.count():
            return []
        self.sort_uncategorized()
        return self.report(cfg.get("brief", "totals"))

    def sort_uncategorized(self) -> int:
        merchants = self.finance.uncategorized_merchants(40)
        if not merchants or self.ctx.settings.dry_run:
            return 0
        from ..code.brain import parse_json_obj

        # Local model only, whatever the provider setting: this is Patrick's spending.
        raw = self.ctx.llm.ask_local("finance:sort", SORT_PROMPT.format(cats=", ".join(CATEGORIES),
                                     merchants="\n".join(f"- {m}" for m in merchants)), json_mode=True, max_tokens=900).text
        got = parse_json_obj(raw) or {}
        n = 0
        for m in merchants:
            cat = got.get(m)
            if cat in CATEGORIES and cat != "Other":
                self.finance.learn(m, cat)
                n += 1
        if n and self.ctx.events is not None:
            self.ctx.events.add("finance", "info", f"Sorted {n} new merchant{'s' if n > 1 else ''} into categories")
        return n

    def report(self, mode: str = "totals", today: date | None = None) -> list:
        if mode == "off":
            return []
        today = today or date.today()
        out = []
        tag = today.isoformat()

        def add(key, title, summary, importance=3):
            f = Finding(cell=self.name, topic="Finance", title=title, summary=summary, url=f"finance:{key}:{tag}",
                        importance=importance)
            if self.ctx.findings.add(f):
                out.append(f)

        money = lambda v: f"${v:,.0f}" if v >= 100 else f"${v:,.2f}"
        due = self.finance.upcoming(7, today)
        if due:
            total = sum(r["amount"] for r in due)
            add("due", f"{len(due)} bill{'s' if len(due) > 1 else ''} or subscription{'s' if len(due) > 1 else ''} due this week",
                (f"About {money(total)} in total. " if mode == "totals" else "") + "Details in Finance.", 3)
        for c in self.finance.month()["by_category"]:
            if c["budget"] and c["spent"] >= 0.9 * c["budget"]:
                pct = round(100 * c["spent"] / c["budget"])
                add(f"budget-{c['category']}", f"{c['category']} is at {pct}% of its monthly budget",
                    (f"{money(c['spent'])} of {money(c['budget'])}. " if mode == "totals" else "") + "Details in Finance.",
                    4 if pct >= 100 else 3)
        fresh = [r for r in self.finance.recurring(today) if r["active"] and r["times"] <= 2
                 and date.fromisoformat(r["first_seen"]) >= today - timedelta(days=70)]
        if fresh:
            per = sum(r["per_month"] for r in fresh)
            add("new-recurring", f"{len(fresh)} new recurring charge{'s' if len(fresh) > 1 else ''} spotted",
                (f"Adds about {money(per)} a month. " if mode == "totals" else "") + "Check they're wanted in Finance.", 3)
        return out
