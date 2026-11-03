"""Intel — Sleeper cell that watches the web for each configured topic.

Topics live in config/intel.toml and are re-read on every run, so new ones can be added
without rebuilding.

Provider (WETWORKS_PROVIDER):
  local  (default, free) SearXNG searches each topic's queries, the local Ollama model picks
         and summarises the best results.
  claude Claude web search (paid).
  grok   Grok web + X search (paid), falling back to Claude on error.
"""

import json
import re
import tomllib
from datetime import date
from pathlib import Path

from ..findings import Finding
from ..llm import BudgetExceeded
from ..local import candidates_block
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


LOCAL_PROMPT = """Topic: {name}
What Patrick wants to know:
{brief}

Today is {today}. Below are fresh search results. Pick at most {max_items} that are genuinely
new, specific and relevant to the topic. Skip ads, listicles, duplicates, and anything already
reported this week:
{seen}

Search results:
{candidates}

Reply with ONLY this JSON object:
{{"findings": [{{"index": <result number>, "title": "short headline", "summary": "1-2 sentences: what happened and why it matters to Patrick", "importance": 1-5}}]}}
importance: 5 = act today, 4 = act this week, 3 = useful to know, 2 = minor, 1 = trivia.
Use an empty list if nothing qualifies. Only use facts stated in the results."""


def parse_findings(text: str) -> list[dict]:
    """Pull the JSON array out of a model reply, tolerating code fences or stray prose."""
    text = text.strip()
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    if fence:
        candidates.insert(0, fence.group(1))
    for pattern in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pattern, text, re.S)
        if m:
            candidates.append(m.group(0))
    for c in candidates:
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("findings"), list):
            data = data["findings"]
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
        if self.ctx.settings.provider == "local":
            return self._run_topic_local(topic)
        return self._run_topic_paid(topic)

    def _store(self, name: str, items: list[dict], max_items: int) -> list[Finding]:
        stored = []
        for item in items[:max_items]:
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
        return stored

    def _run_topic_local(self, topic: dict) -> list[Finding]:
        """Free path: SearXNG for search, local model for judgement."""
        name = topic["name"]
        max_items = int(topic.get("max_items", 3))
        queries = topic.get("queries") or [name]
        time_range = topic.get("time_range", "week")
        results, urls = [], set()
        for q in queries:
            for r in self.ctx.llm.web_search_local(q, time_range=time_range, limit=8):
                if r["url"] not in urls and not self.ctx.findings.seen_url(r["url"]):
                    urls.add(r["url"])
                    results.append(r)
        results = results[:20]
        if not results:
            self.log.info("Intel %r: no fresh search results", name)
            return []
        seen = self.ctx.findings.recent_titles(name) or ["(none)"]
        prompt = LOCAL_PROMPT.format(
            name=name,
            brief=topic.get("brief", name).strip(),
            today=date.today().isoformat(),
            seen="\n".join(f"- {t}" for t in seen),
            candidates=candidates_block(results),
            max_items=max_items,
        )
        reply = self.ctx.llm.ask_local(f"intel:{name}", prompt, system=SYSTEM, json_mode=True, max_tokens=900)
        picked = []
        for item in parse_findings(reply.text):
            try:
                src = results[int(item.get("index"))]
            except (TypeError, ValueError, IndexError):
                continue  # model pointed at a result that doesn't exist; drop it rather than guess
            picked.append({**item, "url": src["url"]})
        stored = self._store(name, picked, max_items)
        self.log.info("Intel %r: %d new from %d results (local, free)", name, len(stored), len(results))
        return stored

    def _run_topic_paid(self, topic: dict) -> list[Finding]:
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
            prefer_grok=self.ctx.settings.provider == "grok",
        )
        stored = self._store(name, parse_findings(reply.text), max_items)
        self.log.info("Intel %r: %d new (model %s, %d searches, $%.3f)",
                      name, len(stored), reply.model, reply.searches, reply.cost_usd)
        return stored
