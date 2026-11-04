"""The Observer cell: turns what the helpers report into a daily digest, playbooks for taught tasks, and
suggestions Patrick approves. Local model only."""

import json
from datetime import datetime, timedelta

from ..findings import Finding
from ..observer import SHOT_PROMPT, Observer
from .base import Cell

DIGEST = """Here is how Patrick's day went, from his own activity tracker (apps, window titles, and one-line
descriptions of his screen). Write a short, friendly digest for him: what he mostly worked on, his focus
blocks, anything that ate time, and one practical suggestion. 4-6 sentences, no lists, no personal details.

{data}"""

PLAYBOOK = """Patrick recorded himself doing a task called "{name}" so his agents can learn it. These are the
steps seen on screen, in order:

{steps}

Write a clear playbook in Markdown: a one-line purpose, then numbered steps (merge repeats, skip noise),
then "Inputs" (what changes each time) and "Could be automated by" (how a script or agent could do it,
and what would still need Patrick). Don't invent steps you can't see."""

IDEAS = """From two weeks of Patrick's activity (summary below), suggest at most 2 things his ops center could
take off his plate: research or prep it could do before he starts, or repetitive work a mission could
automate. Only suggest things clearly supported by the data.

{data}

Reply with ONLY JSON: {{"ideas": [{{"title": "short", "why": "what in the data shows it", "request": "what the mission should do"}}]}}"""


class ObserverCell(Cell):
    name = "observer"
    kind = "sleeper"

    def __init__(self, ctx, observer: Observer, approvals=None):
        super().__init__(ctx)
        self.observer = observer
        self.approvals = approvals

    # ---- screenshots ---------------------------------------------------------------------
    def describe(self, image: bytes, device: str, app: str, title: str, recording: int = 0) -> str | None:
        """One sentence about a screenshot; the image itself is never stored."""
        if self.observer.excluded(app, title) or self.ctx.settings.dry_run:
            return None
        if self.ctx.llm.paused.is_set():  # The Forge has the GPU
            return None
        try:
            text = self.ctx.llm.ollama.describe_image(image, SHOT_PROMPT)
        except Exception as e:
            self.log.info("Couldn't describe screenshot: %s", e)
            return None
        self.observer.add_shot(device, app, title, text, recording)
        return text

    def make_playbook(self, rid: int) -> str:
        r = self.observer.recording(rid)
        if not r:
            raise ValueError("No such recording.")
        steps = "\n".join(f"{i + 1}. [{s['app']}] {s['description']}" for i, s in enumerate(r["steps"])) or "(no steps captured)"
        text = "(dry run)" if self.ctx.settings.dry_run else self.ctx.llm.ask_local(
            "observer:playbook", PLAYBOOK.format(name=r["name"], steps=steps[:9000]), max_tokens=1200).text
        self.observer.set_playbook(rid, text)
        if self.ctx.events is not None:
            self.ctx.events.add("observer", "done", f"Wrote a playbook for “{r['name']}” ({len(r['steps'])} steps seen)")
        return text

    # ---- nightly: digest, tidy up, suggestions ----------------------------------------------------
    def run(self) -> list:
        cfg = self.observer.config()
        if not cfg.get("enabled", True):
            return []
        today = self.observer.day()
        if today["active_minutes"] >= 15:
            data = {k: today[k] for k in ("active_minutes", "first", "last", "apps", "top_windows", "focus_blocks", "switches")}
            data["screens"] = [f"{s['time']} {s['what']}" for s in today["screens"]][-40:]
            text = "(dry run)" if self.ctx.settings.dry_run else self.ctx.llm.ask_local(
                "observer:digest", DIGEST.format(data=json.dumps(data, indent=1)[:9000]), max_tokens=400).text
            self.observer.save_digest(today["day"], text, {k: today[k] for k in ("active_minutes", "focus_blocks", "switches", "first", "last")})
        self.observer.prune()
        self.suggest()
        return []

    def morning_report(self) -> list:
        """For the brief: yesterday in numbers only (no apps or windows)."""
        if self.observer.config().get("brief", "summary") == "off":
            return []
        y = self.observer.day(datetime.now(self.observer.tz).date() - timedelta(days=1))
        if y["active_minutes"] < 30:
            return []
        h = y["active_minutes"] / 60
        f = Finding(cell=self.name, topic="Your day", title=f"Yesterday: {h:.1f}h active, {len(y['focus_blocks'])} focus block"
                    f"{'s' if len(y['focus_blocks']) != 1 else ''}", summary="Your digest is in Observer.",
                    url=f"observer:{y['day']}", importance=2)
        return [f] if self.ctx.findings.add(f) else []

    def suggest(self) -> int:
        """Patterns -> approval cards. Each idea is only ever suggested once."""
        if self.approvals is None:
            return 0
        p = self.observer.patterns()
        if p["days_seen"] < 5:
            return 0  # not enough to go on yet
        n = 0
        s = self.ctx.settings

        def card(key, args, summary, reason):
            nonlocal n
            if self.observer.mark_suggested(key):
                self.approvals.request("observer", "adopt_observation", args, summary, reason=reason)
                n += 1

        def minutes(hm):
            h, m = map(int, hm.split(":"))
            return h * 60 + m

        if p["usual_start"] and abs(minutes(p["usual_start"]) - minutes(s.morning_brief)) > 45:
            t = max(0, minutes(p["usual_start"]) - 15)
            hm = f"{t // 60:02d}:{t % 60:02d}"
            card(f"brief:morning:{hm}", {"kind": "brief_time", "which": "morning", "time": hm},
                 f"Move the morning brief to {hm}", f"Over {p['days_seen']} days you usually start around {p['usual_start']}.")
        if p["usual_end"] and abs(minutes(p["usual_end"]) - minutes(s.evening_brief)) > 60:
            t = min(23 * 60 + 30, minutes(p["usual_end"]) + 15)
            hm = f"{t // 60:02d}:{t % 60:02d}"
            card(f"brief:evening:{hm}", {"kind": "brief_time", "which": "evening", "time": hm},
                 f"Move the evening brief to {hm}", f"You usually wrap up around {p['usual_end']}.")
        if p["morning_routine"]:
            apps = ", ".join(p["morning_routine"])
            card(f"routine:{apps}", {"kind": "mission", "title": "Morning prep",
                                     "request": f"Patrick usually opens his day with {apps}. Work out what he checks there that "
                                                "Wetworks could gather for him before he starts, and propose a morning prep."},
                 "Plan a morning prep around how you start the day", f"On {p['routine_days']} days you started with {apps}.")
        if not s.dry_run:
            days = [self.observer.day(datetime.now(self.observer.tz).date() - timedelta(days=i)) for i in range(1, 8)]
            data = {"patterns": p, "days": [{k: d[k] for k in ("day", "active_minutes", "apps", "top_windows")} for d in days],
                    "digests": [d["text"] for d in self.observer.digests(7)]}
            try:
                from ..code.brain import parse_json_obj

                ideas = (parse_json_obj(self.ctx.llm.ask_local("observer:ideas", IDEAS.format(data=json.dumps(data)[:10000]),
                                                              json_mode=True, max_tokens=600).text) or {}).get("ideas", [])
            except Exception:
                ideas = []
            for idea in ideas[:2]:
                if idea.get("title") and idea.get("request"):
                    card(f"idea:{idea['title'].lower()[:80]}", {"kind": "mission", "title": idea["title"][:80], "request": idea["request"][:1500]},
                         f"Automation idea: {idea['title'][:80]}", str(idea.get("why", ""))[:300])
        return n
