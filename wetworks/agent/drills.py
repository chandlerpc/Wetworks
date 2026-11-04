"""Forge drills: practice runs that bank training lessons faster.

On a timer, C.L.a.i.R (Gemini) and The Architect (also on Gemini for drills) work realistic
practice requests with the real tool list and real Wetworks data. Read-only tools run for real
so the lessons see true results; anything that would change something (open a mission, save a
memory, message Patrick, queue training) is simulated and changes nothing. Every decision is
recorded as a teacher lesson for The Forge.

Drills only ever think on Gemini. If Gemini isn't reachable they skip: the local model's own
decisions are not lessons.
"""

import copy
import itertools
import logging
import random
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import clair as clair_mod
from . import commander as commander_mod
from .runtime import Agent, Toolbox
from .traces import recorded

log = logging.getLogger("wetworks.drills")

# Tools that change something. In drills they answer like the real tool and do nothing.
_ids = itertools.count(900)
SIMULATED = {
    "start_mission": lambda title="", request="", kind="research": (
        f"Opened {'code ' if kind == 'code' else ''}mission #{next(_ids)}: {str(title or request)[:80]}"),
    "research_lead": lambda id="": f"Mission #{next(_ids)} queued to research lead #{id}. The write-up lands on the lead.",
    "note_lead": lambda id="", notes="", dismiss=False: f"Updated lead #{id}.",
    "draft_outreach": lambda id="": (f"Draft saved on lead #{id} (not sent): Subject: Quick idea for your front desk\n\n"
                                     "Hi, I noticed you're hiring... (draft continues)"),
    "remember": lambda text="", kind="fact": "Saved.",
    "follow_up_later": lambda note="", hours=24: f"Follow-up #{next(_ids)} set.",
    "tell_patrick": lambda message="": "Sent to Patrick.",
    "tell_architect": lambda note="": "Passed to The Architect; he'll pick it up within a few minutes.",
    "build_training_data": lambda: {"name": "agent-steps", "train": 0, "holdout": 0, "needed": 150},
    "queue_training": lambda name="", dataset="agent-steps": f"Queued training job {next(_ids)} for tonight's window (1-5 local).",
    # Never real money data in a drill: drills think on Gemini.
    "finance_summary": lambda: {"month": "sample", "spent_this_month": 1234.56, "by_category": [
        {"category": "Groceries", "spent": 410.0, "budget": 600}, {"category": "Dining", "spent": 285.0, "budget": 300}],
        "recurring_monthly_total": 96.4, "due_this_week": {"count": 2, "total": 61.98}, "categories_near_budget": ["Dining"]},
    "benchmark_model": lambda model="", dataset="agent-steps": f"Benchmark of {model} queued; results show in forge_status.",
}


class _SimApprovals:
    """Gated tools in drills: looks like a request went to Patrick; nothing is stored."""
    def request(self, agent, tool, args, summary, reason=""):
        return {"id": next(_ids)}

    def pending(self):
        return []


def drill_toolbox(real: Toolbox) -> Toolbox:
    """Same tool names and descriptions as the real agent sees (so lessons transfer), safe behaviour."""
    out = Toolbox()
    for t in real.tools.values():
        t2 = copy.copy(t)
        if t.name in SIMULATED:
            t2.fn = SIMULATED[t.name]
        elif t.gate != "approve" and t.name not in READ_ONLY:
            t2.fn = lambda **kw: "Done."  # unknown write tool: never run it for real
        out.add(t2)
    return out


READ_ONLY = {"search_web", "read_page", "recall", "search_past_chats", "read_past_chat", "list_missions",
             "get_mission", "recent_findings", "list_leads", "forge_status"}


# ---- practice requests ---------------------------------------------------------------------
# Written the way Patrick talks to them: short, often by voice, sometimes vague.
CLAIR = [
    "Hey, what's going on today?", "Anything new from Intel?", "How much have we spent this month?",
    "What missions are running right now?", "Did the clinic research finish?", "Any good leads this morning?",
    "Which lead should I reach out to first?", "Who do I need to follow up with?", "Remind me what Sunrise Spa needed.",
    "Look up what the latest Gemini model is called.", "What's the weather like for climbing this weekend in Orlando?",
    "Start a mission to find five Sanford businesses hiring receptionists.",
    "Can you research gyms in Lake Mary that still use paper waivers?",
    "Remember that I only want leads in Seminole County for now.", "Remember I'm off on Fridays from now on.",
    "What did we decide about the Forge base model?", "Is the Forge training anything?", "How close is the Forge to training?",
    "Tell the Architect to focus on revenue this week.", "Have the Architect plan out the Day Job room.",
    "What's on my plate today?", "Give me a quick status.", "Cancel that, never mind.", "Thanks Clair, that's all.",
    "What did Intel say about AI models yesterday?", "Any job postings worth a look?", "Find me cheap GPU cloud options for fine tuning.",
    "Draft something for Lake Mary Family Dental.", "Mark the dental lead as not a fit.", "Write the outreach for the best lead.",
    "Did anything break overnight?", "What's waiting on my approval?", "Why is the Architect blocked?",
    "Search my old chats for what I said about SummitFlow pricing.", "What did I say about BDO in past chats?",
    "Open a code mission to add a dark mode toggle to the console.", "How many leads have I contacted?",
    "What's the best time to send outreach emails to small businesses?", "Look up Novarad NovaPACS release notes.",
    "Set a reminder to check the Forge tomorrow.", "Is Gemini or the local model answering you right now?",
    "Find three florida grants for small tech businesses.", "Compare Qwen3 4B and Gemma for agent tasks.",
    "Who is Gregg again?", "What do you know about my freelance business?", "Summarize the last mission.",
    "hey clair", "you there?", "what's the Revenue cell doing", "any money opportunities", "start intel now",
]
ARCHITECT = [
    "What should we focus on this week?", "Plan how we get the first paying client this month.",
    "Review the open leads and tell me which two are worth research.", "Is anything stuck?",
    "Give me a status report.", "Should we train a model this week or wait for more data?",
    "What would it take to unseal the Day Job room?", "Line up research for the strongest lead.",
    "Check on the missions from yesterday and decide next steps.", "Are we spending money anywhere we shouldn't?",
    "Open a mission to compare outreach email tools that are free.", "What's the plan for Friday when Claude comes online?",
    "Do we have any follow-ups due?", "Which Intel topic is giving us the least value?",
    "Merge the latest pull request if the tests passed.", "Switch the agents to the newest Forge model.",
]
TEMPLATES = [  # filled with real names when there are any
    ("clair", "What do we know about {lead}?"), ("clair", "Should I reach out to {lead}?"),
    ("clair", "Research {lead} for me."), ("clair", "I emailed {lead} yesterday, keep track of it."),
    ("architect", "Is {lead} worth a mission?"), ("clair", "What happened with mission {mission}?"),
    ("architect", "What's the next step after mission {mission}?"),
]


class Drills:
    def __init__(self, *, path: Path, traces, bridge, clair_tools: Toolbox, architect_tools: Toolbox, context_fn,
                 events=None, leads=None, missions=None, settings=None, lessons_fn=None):
        self.traces, self.bridge, self.events = traces, bridge, events
        self.context_fn, self.leads, self.missions, self.s = context_fn, leads, missions, settings
        self.lessons_fn = lessons_fn  # () -> {"have": n, ...}
        self.tools = {"clair": drill_toolbox(clair_tools), "architect": drill_toolbox(architect_tools)}
        self.halt = None
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS drills (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
            agent TEXT NOT NULL, request TEXT NOT NULL, steps INTEGER NOT NULL, ok INTEGER NOT NULL, note TEXT NOT NULL)""")
        self.db.commit()
        self.blocked_until = ""  # Gemini rate-limited: wait until this ISO date

    # ---- bookkeeping ------------------------------------------------------------------
    def _today(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def steps_today(self) -> int:
        r = self.db.execute("SELECT COALESCE(SUM(steps), 0) FROM drills WHERE ts >= ?", (self._today(),)).fetchone()
        return int(r[0])

    def stats(self) -> dict:
        total, ok = self.db.execute("SELECT COUNT(*), COALESCE(SUM(ok), 0) FROM drills").fetchone()
        return {"drills": total, "completed": int(ok), "steps_today": self.steps_today(),
                "daily_steps": self.s.drill_daily_steps if self.s else 0, "paused_until": self.blocked_until or None}

    def _log(self, agent, request, steps, ok, note):
        self.db.execute("INSERT INTO drills (ts, agent, request, steps, ok, note) VALUES (?, ?, ?, ?, ?, ?)",
                        (datetime.now(timezone.utc).isoformat(), agent, request, steps, int(ok), note[:300]))
        self.db.commit()

    # ---- choosing a request -----------------------------------------------------------
    def candidates(self) -> list[tuple[str, str]]:
        pool = [("clair", r) for r in CLAIR] + [("architect", r) for r in ARCHITECT]
        leads = [l["business"] for l in (self.leads.recent(limit=12) if self.leads else [])]
        missions = [str(m["id"]) for m in (self.missions.recent(10) if self.missions else [])]
        for agent, t in TEMPLATES:
            if "{lead}" in t:
                pool += [(agent, t.format(lead=l)) for l in leads[:6]]
            elif "{mission}" in t:
                pool += [(agent, t.format(mission=f"#{m}")) for m in missions[:4]]
        pool += [("architect", "__cycle__")]  # a full command cycle
        return pool

    def pick(self) -> tuple[str, str]:
        used = dict(self.db.execute("SELECT request, COUNT(*) FROM drills GROUP BY request").fetchall())
        pool = self.candidates()
        least = min(used.get(r, 0) for _, r in pool)
        return random.choice([(a, r) for a, r in pool if used.get(r, 0) == least])

    # ---- running ----------------------------------------------------------------------
    def ready(self) -> tuple[bool, str]:
        if self.bridge is None or not self.bridge.configured:
            return False, "C.L.a.i.R's Cloudflare bridge isn't set up, so there's no Gemini to learn from"
        if self.halt is not None and self.halt.is_set():
            return False, "Wetworks is halted"
        if self.blocked_until and self._today() < self.blocked_until:
            return False, f"Gemini's free daily limit was hit; drills resume {self.blocked_until}"
        if self.s and self.steps_today() >= self.s.drill_daily_steps:
            return False, f"Daily drill budget used ({self.s.drill_daily_steps} Gemini calls)"
        if self.events is not None and ("clair" in self.events.busy or "architect" in self.events.busy):
            return False, "an agent is busy with real work"
        return True, ""

    def tick(self) -> None:
        """Scheduler hook: one drill if it's drill time and there's budget."""
        if not (self.s and self.s.drill_enabled):
            return
        hours = self.s.drill_hours
        start, end = (int(x) for x in hours.split("-"))
        h = datetime.now(ZoneInfo(self.s.timezone)).hour
        if not (start <= h < end):
            return
        if self.lessons_fn and self.lessons_fn()["have"] >= self.s.drill_target:
            return
        ok, _ = self.ready()
        if ok:
            self.run_one()

    def run_one(self, agent: str | None = None, request: str | None = None) -> dict:
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "note": "A drill is already running."}
        try:
            ok, why = self.ready()
            if not ok:
                return {"ok": False, "note": why}
            if request is None:
                agent, request = self.pick()
            agent = agent or "clair"
            calls = {"n": 0}

            def gemini(system, prompt, max_tokens):
                calls["n"] += 1
                return self.bridge.step(system, prompt, max_tokens)  # raises on failure: nothing recorded

            ask = recorded(gemini, self.traces, f"drill:{agent}", lambda: "gemini")
            context = self.context_fn()
            if agent == "architect" and request == "__cycle__":
                a = Agent("architect", commander_mod.PERSONA, self.tools["architect"], ask, approvals=_SimApprovals(),
                          max_steps=6)
                goal = commander_mod.CYCLE_GOAL.format(reason="scheduled check")
            elif agent == "architect":
                a = Agent("architect", commander_mod.PERSONA, self.tools["architect"], ask, approvals=_SimApprovals(),
                          max_steps=5)
                goal = commander_mod.CHAT_GOAL.format(transcript=f"Patrick: {request}")
            else:
                a = Agent("clair", clair_mod.PERSONA, self.tools["clair"], ask, approvals=_SimApprovals(), max_steps=5)
                goal = clair_mod.GOAL.format(transcript=f"Patrick: {request}")
            try:
                run = a.run(goal, context)
            except Exception as e:
                note = str(e)
                if "limit" in note.lower() or "429" in note:
                    # free tier resets daily; try again tomorrow
                    self.blocked_until = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
                self._log(agent, request, calls["n"], False, note)
                if self.events is not None:
                    self.events.add("forge", "warn", f"Drill stopped: {note[:160]}")
                return {"ok": False, "note": note, "steps": calls["n"]}
            self._log(agent, request, calls["n"], True, run.final[:300])
            label = "command cycle" if request == "__cycle__" else f"“{request[:60]}”"
            if self.events is not None:
                self.events.add("forge", "info", f"Drill ({'C.L.a.i.R' if agent == 'clair' else 'The Architect'}): "
                                f"{label}, {calls['n']} lessons")
            return {"ok": True, "agent": agent, "request": request, "steps": calls["n"], "final": run.final}
        finally:
            self.lock.release()
