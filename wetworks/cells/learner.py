"""Learner — nightly Sleeper that keeps Wetworks' memory current.

Reads what Patrick said in chat today and the missions he asked for, and keeps the durable
facts he stated (preferences, decisions, projects, people, facts about him). Skips anything
temporary or sensitive. Also archives mission details older than a year, with a heads-up in the
brief a week before.
"""

from datetime import datetime, timedelta, timezone

from ..findings import Finding
from ..memory import KINDS, archive_old_missions
from .base import Cell
from .intel import parse_findings

SYSTEM = (
    "You maintain the long-term memory of Wetworks, Patrick's personal multi-agent system. You keep "
    "only durable things Patrick himself stated, written as one plain sentence each. You never guess, "
    "infer personality, or add anything he didn't say."
)

PROMPT = """Today Patrick said these things to his assistants:
{said}

He asked for these missions:
{missions}

Already remembered (don't repeat these or near-duplicates):
{known}

List the durable facts worth remembering for months: his preferences, decisions, projects, goals,
people he works with and their roles, and facts about him or his business. Skip greetings, one-off
requests, status questions, anything that will be stale within a week, and anything about health,
money amounts, passwords, keys or account numbers.
Reply with ONLY this JSON object:
{{"memories": [{{"title": "one plain sentence, written about Patrick in the third person",
  "kind": "preference" or "fact" or "project" or "person" or "decision"}}]}}
Use an empty list if nothing qualifies. At most 8."""


class Learner(Cell):
    name = "learner"
    kind = "sleeper"

    def __init__(self, ctx, comms, missions):
        super().__init__(ctx)
        self.comms = comms
        self.missions = missions

    def _ask(self, prompt: str) -> str:
        llm = self.ctx.llm
        if self.ctx.settings.provider == "local":
            return llm.ask_local("learner", prompt, system=SYSTEM, json_mode=True, max_tokens=900).text
        return llm.ask("learner", prompt, tier="route", system=SYSTEM, max_tokens=900).text

    def run(self, now: datetime | None = None) -> list[Finding]:
        now = now or datetime.now(timezone.utc)
        found: list[Finding] = []
        mem = self.ctx.memory
        since = (now - timedelta(hours=24)).isoformat()

        said = [m["text"] for m in self.comms.messages_since(since, sender="patrick")]
        asked = []
        with self.missions.lock:
            rows = self.missions.db.execute(
                "SELECT title, request FROM missions WHERE created >= ? ORDER BY id", (since,)).fetchall()
        asked = [f"{t}: {r[:300]}" for t, r in rows]

        learned = 0
        if mem is not None and (said or asked):
            known = [m["text"] for m in mem.recent(60)]
            raw = self._ask(PROMPT.format(
                said="\n".join(f"- {s[:400]}" for s in said[-60:]) or "(nothing)",
                missions="\n".join(f"- {a}" for a in asked[-20:]) or "(none)",
                known="\n".join(f"- {k}" for k in known) or "(nothing yet)",
            ))
            for item in parse_findings(raw)[:8]:
                kind = str(item.get("kind", "fact"))
                if mem.add(str(item["title"]), kind=kind if kind in KINDS else "fact", source="nightly"):
                    learned += 1
            if learned and self.ctx.events is not None:
                self.ctx.events.add("learner", "info", f"Learned {learned} new thing(s) from today")

        archived, due = archive_old_missions(self.missions.db, self.missions.lock, now=now)
        if archived and self.ctx.events is not None:
            self.ctx.events.add("learner", "info", f"Archived {archived} mission(s) older than a year")
        if due:
            f = Finding(cell=self.name, topic="Housekeeping",
                        title=f"{due} mission(s) will be archived next week",
                        summary="Their details move to the archive after a year. Open Missions if you want to save anything first.",
                        importance=3)
            if self.ctx.findings.add(f):
                found.append(f)
        self.log.info("Learner: %d learned, %d archived, %d due next week", learned, archived, due)
        return found
