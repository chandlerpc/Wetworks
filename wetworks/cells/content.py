"""Content cell (The Writer). Turns what Patrick has been building and reading into post ideas and drafts.
Drafts are written by the local model and are never posted by Wetworks. Drafts that mention anything on the
never_mention list (work, patients, money, family) are flagged for Patrick."""

from datetime import datetime, timedelta, timezone

from ..content import CHANNELS, Content
from ..findings import Finding
from .base import Cell

IDEAS = """You help Patrick Caldwell (software and AI developer, Sanford FL, runs CodeVoyage) plan posts.
His content pillars:
{pillars}

What he's been doing and reading lately:
{material}

Ideas he already has (don't repeat): {existing}

Suggest {n} post ideas grounded in the material above (no generic topics). Each needs a concrete angle:
the specific thing built, learned, measured or decided. Never involve his day job, patients, money or family.
Reply with ONLY JSON: {{"ideas": [{{"title": "...", "angle": "...", "pillar": "...", "source": "what in the material"}}]}}"""

FORMATS = {
    "linkedin": "a LinkedIn post: 120-220 words, a strong first line, short paragraphs, at most two hashtags at the end",
    "x": "an X post: under 270 characters, one idea, no hashtags",
    "blog": "a blog post outline plus intro: a title, a 2-paragraph intro, then 4-6 section headings with one line each",
    "portfolio": "a portfolio project update: 60-120 words, what it is, what's new, the stack, one result",
}

DRAFT = """Write {format} for Patrick.

Idea: {title}
Angle: {angle}

Voice: {voice}

Rules: only claim things in the idea and angle; no invented numbers, clients or results; nothing about his day
job, patients, money or family. Reply with just the post text."""


class ContentCell(Cell):
    name = "content"
    kind = "sleeper"

    def __init__(self, ctx, content: Content, missions=None, observer=None):
        super().__init__(ctx)
        self.content = content
        self.missions = missions
        self.observer = observer

    def material(self) -> str:
        parts = []
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        if self.missions is not None:
            done = [m for m in self.missions.recent(30) if m["status"] == "done" and m["updated"] >= since]
            parts += [f"- Finished research: {m['title']}: {m['result_preview'][:200]}" for m in done[:6]]
        try:
            for f in self.ctx.findings.recent(days=7, limit=40):
                if f.get("topic") in ("AI models & tools", "Money opportunities", "Job market") and f.get("importance", 0) >= 3:
                    parts.append(f"- Read: {f['title']}: {f['summary'][:160]}")
        except Exception:
            pass
        if self.observer is not None:
            parts += [f"- His day {d['day']}: {d['text'][:300]}" for d in self.observer.digests(5)]
        return "\n".join(parts[:25]) or "(nothing recorded this week)"

    def _ask(self, agent: str, prompt: str, max_tokens: int, json_mode: bool = False) -> str:
        if self.ctx.settings.dry_run:
            return '{"ideas": []}' if json_mode else "(dry run draft)"
        return self.ctx.llm.ask_local(agent, prompt, json_mode=json_mode, max_tokens=max_tokens).text  # local model only

    def new_ideas(self, n: int | None = None) -> list[dict]:
        cfg = self.content.config()
        n = n or int(cfg.get("ideas_per_run", 3))
        from ..code.brain import parse_json_obj

        raw = self._ask("content:ideas", IDEAS.format(pillars="\n".join(f"- {p}" for p in cfg.get("pillars", [])),
                        material=self.material(), existing="; ".join(p["title"] for p in self.content.pieces(limit=30)) or "(none)", n=n),
                        900, json_mode=True)
        out = []
        for idea in (parse_json_obj(raw) or {}).get("ideas", [])[:n]:
            if not isinstance(idea, dict) or self.content.flagged(f"{idea.get('title', '')} {idea.get('angle', '')}"):
                continue
            p = self.content.add_idea(str(idea.get("title", "")), str(idea.get("angle", "")), "the writer: " + str(idea.get("source", ""))[:60],
                                      str(idea.get("pillar", "")))
            if p:
                out.append(p)
        return out

    def draft(self, pid: int, channels: list[str] | None = None) -> dict:
        p = self.content.get(pid)
        if not p:
            raise ValueError("No such idea.")
        cfg = self.content.config()
        for ch in channels or self.content.channels():
            if ch in FORMATS:
                text = self._ask(f"content:draft:{ch}", DRAFT.format(format=FORMATS[ch], title=p["title"], angle=p["angle"] or "(none given)",
                                                                     voice=cfg.get("voice", "")), 700)
                self.content.set_draft(pid, ch, text)
        p = self.content.get(pid)
        if self.ctx.events is not None:
            self.ctx.events.add("content", "warn" if p["flags"] else "done",
                                f"Drafted “{p['title'][:60]}”" + (f" (check it: mentions {p['flags']})" if p["flags"] else ""))
        return p

    def run(self) -> list:
        cfg = self.content.config()
        if not cfg.get("enabled", True):
            return []
        self.new_ideas()
        ideas = self.content.pieces("idea", limit=50)[::-1]  # oldest first
        for p in ideas[: int(cfg.get("drafts_per_run", 1))]:
            self.draft(p["id"])
        ready = len(self.content.pieces("draft"))
        if not ready:
            return []
        f = Finding(cell=self.name, topic="Content", title=f"{ready} post draft{'s' if ready > 1 else ''} ready for review",
                    summary="Nothing is posted until you copy it. Details in Content.",
                    url=f"content:ready:{datetime.now(timezone.utc).date()}", importance=2)
        return [f] if self.ctx.findings.add(f) else []
