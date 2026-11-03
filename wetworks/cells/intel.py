"""Intel — Sleeper cell that watches the web for each configured topic.

Topics live in config/intel.toml and are re-read on every run, so new ones can be added
without rebuilding. Uses Grok (web + X search) when XAI_API_KEY is set, Claude otherwise.
"""

import json
import re
import tomllib
from datetime import date
from pathlib import Path

from ..findings import Finding
from ..llm import BudgetExceeded
from .base import Cell

SYSTEM = (
    "You are an Intel operative in Wetworks. You search the web and report only verifiable, "
    "recent, specific findings with a source URL. Never invent sources. If nothing new and "
    "relevant turned up, return an empty list."
)

PROMPT = """Topic: {name}
What to look for:
{brief}

Today is {today}. Focus on the last few days. Already reported this week (skip these):
{seen}

Search the web, then reply with ONLY a JSON array (no prose, no code fence) of at most {max_items}
objects, most important first:
[{{"title": "...", "summary": "1-2 sentences: what happened and why it matters to Patrick",
  "url": "https://...", "importance": 1-5}}]
importance: 5 = act today, 4 = act this week, 3 = useful to know, 2 = minor, 1 = trivia."""


def parse_findings(text: str) -> list[dict]:
    """Pull the JSON array out of a model reply, tolerating code fences or stray prose."""
    text = text.strip()
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    if fence:
        candidates.insert(0, fence.group(1))
    bracket = re.search(r"\[.*\]", text, re.S)
    if bracket:
        candidates.append(bracket.group(0))
    for c in candidates:
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict) and d.get("title")]
    return []


class Intel(Cell):
    name = "intel"
    kind = "sleeper"

    def __init__(self, ctx, topics_path: Path | None = None):
        super().__init__(ctx)
        self.topics_path = topics_path or ctx.settings.config_dir / "intel.toml"
        self.config = self._load()

    def _load(self) -> dict:
        with open(self.topics_path, "rb") as f:
            cfg = tomllib.load(f)
        start = cfg.get("intel", {}).get("trial_start")
        self.trial_start = date.fromisoformat(str(start)) if start else None
        return cfg

    @property
    def topics(self) -> list[dict]:
        return [t for t in self.config.get("topics", []) if t.get("enabled", True)]

    def run(self) -> list[Finding]:
        self.config = self._load()  # pick up edits without a restart
        new: list[Finding] = []
        for topic in self.topics:
            try:
                new.extend(self._run_topic(topic))
            except BudgetExceeded:
                self.log.warning("Budget reached; Intel stopping early")
                break
            except Exception:
                self.log.exception("Intel topic %r failed", topic.get("name"))
        return new

    def _run_topic(self, topic: dict) -> list[Finding]:
        name = topic["name"]
        max_items = int(topic.get("max_items", 3))
        seen = self.ctx.findings.recent_titles(name) or ["(none)"]
        prompt = PROMPT.format(
            name=name,
            brief=topic.get("brief", name).strip(),
            today=date.today().isoformat(),
            seen="\n".join(f"- {t}" for t in seen),
            max_items=max_items,
        )
        reply = self.ctx.llm.search(
            f"intel:{name}",
            prompt,
            tier=self.ctx.settings.intel_tier,
            system=SYSTEM,
            max_searches=int(topic.get("searches", 3)),
            prefer_grok=True,
        )
        stored = []
        for item in parse_findings(reply.text)[:max_items]:
            try:
                importance = max(1, min(5, int(item.get("importance", 3))))
            except (TypeError, ValueError):
                importance = 3
            f = Finding(
                cell=self.name,
                topic=name,
                title=str(item["title"]).strip()[:200],
                summary=str(item.get("summary", "")).strip()[:600],
                url=str(item.get("url", "")).strip(),
                importance=importance,
            )
            if self.ctx.findings.add(f):
                stored.append(f)
        self.log.info("Intel %r: %d new (model %s, %d searches, $%.3f)",
                      name, len(stored), reply.model, reply.searches, reply.cost_usd)
        return stored
