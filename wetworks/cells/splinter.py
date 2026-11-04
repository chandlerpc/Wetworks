"""Splinter — an on-demand strike team for one mission.

Flow, all on the free local model by default:
  1. Plan: decide whether the mission needs web research, and which searches to run.
  2. Research (if needed): SearXNG searches, then read the most promising pages.
  3. Write: produce the deliverable, citing sources by number.
Sources always come from real search results; the model only refers to them by index.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date


log = logging.getLogger("wetworks.splinter")

SYSTEM = (
    "You are a Splinter operative in Wetworks, Patrick's multi-agent ops center, working a single "
    "mission for him. Be concrete and practical. Never invent facts, numbers, people or sources. "
    "Patrick: software and AI developer in Central Florida, runs a small AI/software consultancy, "
    "works in healthcare imaging software support."
)

PLAN_PROMPT = """Mission: {title}
Patrick's request:
{request}

Today is {today}. Decide how to do this. Reply with ONLY this JSON object:
{{"needs_research": true or false,
  "queries": ["up to 3 specific web searches, only if research is needed"],
  "deliverable": "one sentence: what you will hand Patrick at the end"}}
Research is needed for anything about current events, prices, people, companies, jobs, products
or facts you'd have to look up. Pure writing or thinking tasks don't need it."""

WRITE_PROMPT = """Mission: {title}
Patrick's request:
{request}

{known}

What to deliver: {deliverable}

{sources_block}

Write the deliverable now. Plain text with short paragraphs or "- " bullets; "## " for headings
if it's long. {cite_rule} Keep it as short as the task allows (under 450 words unless the request
clearly needs more). End with a line starting "Next step:" with one concrete action for Patrick."""


@dataclass
class MissionResult:
    text: str
    sources: list[dict] = field(default_factory=list)


class Cancelled(Exception):
    pass


class Splinter:
    name = "splinter"
    kind = "splinter"
    MAX_RESULTS = 15
    READ_PAGES = 4

    def __init__(self, ctx, store):
        self.ctx = ctx
        self.store = store

    # -- model helper: free local by default, paid only if the provider says so --
    def _ask(self, mid: int, prompt: str, json_mode: bool, max_tokens: int) -> str:
        llm = self.ctx.llm
        agent = f"splinter:{mid}"
        if self.ctx.settings.provider == "local":
            return llm.ask_local(agent, prompt, system=SYSTEM, json_mode=json_mode, max_tokens=max_tokens).text
        return llm.ask(agent, prompt, tier="work", system=SYSTEM, max_tokens=max_tokens).text

    def _check(self, mid: int) -> None:
        if mid in self.store.cancel_requested:
            raise Cancelled()

    def _step(self, mid: int, text: str) -> None:
        self.store.add_step(mid, text)
        if self.ctx.events is not None:
            self.ctx.events.add("splinter", "info", f"Mission #{mid}: {text}")

    def run(self, mission: dict) -> MissionResult:
        mid = mission["id"]
        today = date.today().isoformat()

        # 1. Plan
        self._step(mid, "Planning the approach")
        raw = self._ask(mid, PLAN_PROMPT.format(title=mission["title"], request=mission["request"], today=today),
                        json_mode=True, max_tokens=300)
        plan = _parse_obj(raw)
        queries = [q for q in plan.get("queries", []) if isinstance(q, str) and q.strip()][:3]
        needs = bool(plan.get("needs_research")) and bool(queries)
        deliverable = str(plan.get("deliverable") or "A clear, useful answer to the request.")
        self._check(mid)

        # 2. Research
        sources: list[dict] = []
        if needs:
            seen = set()
            for q in queries:
                self._step(mid, f"Searching: {q}")
                for r in self.ctx.llm.web_search_local(q, time_range="month", limit=6):
                    if r["url"] not in seen:
                        seen.add(r["url"])
                        sources.append(r)
                self._check(mid)
            sources = sources[: self.MAX_RESULTS]
            for s in sources[: self.READ_PAGES]:
                text = self.ctx.llm.read_page_local(s["url"], max_chars=2500)
                if text:
                    s["page"] = text
            read = sum(1 for s in sources if s.get("page"))
            self._step(mid, f"Collected {len(sources)} results and read {read} pages")
            self._check(mid)

        # 3. Write
        self._step(mid, "Writing it up")
        mem = getattr(self.ctx, "memory", None)
        recall = mem.context_for(mission["request"], pinned_limit=15, related_limit=6) if mem else ""
        known = (f"What you know about Patrick (use it to tailor the answer; don't repeat it back):\n{recall}"
                 if recall else "")
        if sources:
            blocks = []
            for i, s in enumerate(sources):
                body = s.get("page") or s.get("content", "")
                blocks.append(f"[{i + 1}] {s['title']}\n{body[:2500]}")
            sources_block = "Sources (numbered):\n" + "\n\n".join(blocks)
            cite_rule = "Back up facts with source numbers like [2]. Use only facts from the sources."
        else:
            sources_block = "No web research was done for this mission."
            cite_rule = "Don't state current facts you can't be sure of; say what Patrick should verify."
        text = self._ask(mid, WRITE_PROMPT.format(title=mission["title"], request=mission["request"],
                                                  deliverable=deliverable, sources_block=sources_block,
                                                  cite_rule=cite_rule, known=known),
                         json_mode=False, max_tokens=1400).strip()
        if not text:
            raise RuntimeError("The model returned an empty write-up")
        public_sources = [{"n": i + 1, "title": s["title"], "url": s["url"]} for i, s in enumerate(sources)]
        return MissionResult(text=text, sources=public_sources)


def _parse_obj(text: str) -> dict:
    import re

    for candidate in (text, *(m.group(0) for m in [re.search(r"\{.*\}", text or "", re.S)] if m)):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, TypeError):
            continue
    return {}
