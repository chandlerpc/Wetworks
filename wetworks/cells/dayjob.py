"""Day Job cell (The Technician). Helps with Novarad support tickets using Patrick's own library of
notes and fixes. Local model only; tickets are already scrubbed of patient details when stored.
The brief only gets counts ("2 tickets open"), never ticket text."""

import json
from datetime import datetime, timedelta, timezone

from ..dayjob import DayJob
from ..findings import Finding
from .base import Cell

SYSTEM = ("You are The Technician, helping Patrick with tech support for Novarad medical imaging software "
          "(NovaPACS, NovaRIS) at hospitals and clinics. Be practical and specific. Prefer what Patrick's own notes and "
          "past fixes say; if they don't cover it, say what you'd check, clearly marked as general advice. Never invent "
          "Novarad menu names, settings or version numbers you weren't given. Text in [BRACKETS REMOVED] was redacted "
          "for privacy; don't guess it.")

DRAFT = """Ticket ({product}):
{ticket}

From Patrick's library (cite by [n] when you use one):
{library}

Reply with ONLY this JSON:
{{"summary": "one line: what's wrong",
  "likely_cause": "best guess and why",
  "steps": ["troubleshooting steps in order"],
  "ask_customer": ["questions to ask if needed"],
  "reply": "a short, friendly reply Patrick could send to the customer (no patient details)",
  "used": [numbers of library items you relied on]}}"""


class DayJobCell(Cell):
    name = "dayjob"
    kind = "sleeper"

    def __init__(self, ctx, dayjob: DayJob):
        super().__init__(ctx)
        self.dayjob = dayjob

    def help(self, tid: int) -> dict:
        t = self.dayjob.ticket(tid)
        if not t:
            raise ValueError("No such ticket.")
        hits = self.dayjob.search(t["text"], limit=5)
        lib = "\n\n".join(f"[{i + 1}] ({h['kind']}) {h['title']}\n{h['body'][:900]}" for i, h in enumerate(hits)) or "(nothing relevant yet)"
        if self.ctx.settings.dry_run:
            data = {"summary": "(dry run)", "likely_cause": "", "steps": [], "ask_customer": [], "reply": "", "used": []}
        else:
            from ..code.brain import parse_json_obj

            # Local model only: this is work material.
            raw = self.ctx.llm.ask_local("dayjob:help", DRAFT.format(product=t["product"], ticket=t["text"][:6000], library=lib),
                                         system=SYSTEM, json_mode=True, max_tokens=1200).text
            data = parse_json_obj(raw) or {"summary": "Couldn't put a draft together; try again.", "steps": []}
        used = [hits[n - 1] for n in data.get("used", []) if isinstance(n, int) and 0 < n <= len(hits)]
        data["sources"] = [{"kind": h["kind"], "ref": h["ref"], "title": h["title"]} for h in (used or hits[:3])]
        self.dayjob.update_ticket(tid, help=json.dumps(data))
        if self.ctx.events is not None:
            self.ctx.events.add("dayjob", "done", f"Drafted help for ticket #{tid} ({t['product']})")
        return data

    def run(self) -> list:
        """Morning: count what's open; never ticket text."""
        open_ = self.dayjob.tickets("open") + self.dayjob.tickets("waiting")
        if not open_:
            return []
        stale = [t for t in open_ if t["updated"] < (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()]
        f = Finding(cell=self.name, topic="Day Job", title=f"{len(open_)} support ticket{'s' if len(open_) > 1 else ''} still open",
                    summary=(f"{len(stale)} untouched for 2+ days. " if stale else "") + "Details in Day Job.",
                    url=f"dayjob:open:{datetime.now(timezone.utc).date()}", importance=3 if stale else 2)
        return [f] if self.ctx.findings.add(f) else []
