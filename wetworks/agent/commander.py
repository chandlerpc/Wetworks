"""The Architect as commander.

Runs on a regular cycle during the day and shortly after missions finish. Each cycle he looks
at the state of Wetworks against Patrick's priorities, decides what's worth doing, opens and
follows up on missions, keeps notes, and tells Patrick only what he needs to know.
He also answers Patrick in the Architect chat channel, using the same tools.
"""

import logging
import threading
import time

from .runtime import Agent

log = logging.getLogger("wetworks.commander")

PERSONA = (
    "You are The Architect, overseer of Wetworks, Patrick's personal multi-agent ops center. "
    "You command Splinter teams (missions) and Sleeper cells (Intel, Revenue) on Patrick's behalf. "
    "Mission priorities, in order: make money, make the day job easier, manage finances, free up "
    "time, build games, create content. Be decisive but economical: every mission uses the MSI's "
    "GPU, so only open work that clearly moves a priority forward, and never open a mission that "
    "duplicates one already queued, running or recently done. You may research, plan, open missions, "
    "draft outreach and take notes on your own. Anything that leaves Wetworks or can't be undone "
    "(sending to other people, merging code, spending money) needs Patrick's approval. Before answering how "
    "Wetworks works or planning any change to it, check read_handbook: it records how every part works, the "
    "rules Patrick set and why."
)

CYCLE_GOAL = """Run a command cycle ({reason}).
1. Check what changed: recent findings, leads, missions, and any follow-ups due.
2. Decide what, if anything, is worth doing now for Patrick's priorities. Good moves: research a
   strong lead before Patrick reaches out, follow up on a finished mission's next step, dig into an
   important Intel finding. Open at most 2 missions this cycle. Doing nothing is fine if nothing matters.
3. Leave follow-up notes for things to check later.
4. Finish with done: a 2-4 line report of what you looked at, what you did and why. Use tell_patrick
   only for something he should act on today."""

CHAT_GOAL = """Patrick wrote to you in the ops console. Conversation so far:
{transcript}

Reply to his latest message. Use tools to look things up or to act (open a mission for real work).
Keep it to a few sentences unless he asks for more. Your done text is the reply he'll see."""


class Commander:
    def __init__(self, *, toolbox, ask, approvals, followups, events, context_fn, memory, max_steps=8,
                 min_gap_minutes=15, paused=None, chat_steps=5, step_tokens=900, uses_gpu=True):
        self.paused = paused  # threading.Event set while The Forge trains
        self.halt = None      # wetworks.halt.Halt
        self.toolbox = toolbox
        self.ask = ask
        self.approvals = approvals
        self.followups = followups
        self.events = events
        self.context_fn = context_fn
        self.memory = memory
        self.max_steps = max_steps
        self.chat_steps = chat_steps
        self.step_tokens = step_tokens
        self.uses_gpu = uses_gpu  # on Claude he doesn't need the GPU, so Forge training doesn't pause him
        self.min_gap = min_gap_minutes * 60
        self.last_cycle = 0.0
        self.lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def agent(self, steps: int | None = None) -> Agent:
        return Agent("architect", PERSONA, self.toolbox, self.ask, approvals=self.approvals,
                     events=self.events, max_steps=steps or self.max_steps, step_tokens=self.step_tokens)

    def _context(self, query: str = "") -> str:
        parts = [self.context_fn()]
        due = self.followups.due()
        if due:
            parts.append("Follow-ups due now:\n" + "\n".join(f"- #{f['id']}: {f['note']}" for f in due))
        pending = self.approvals.pending()
        if pending:
            parts.append("Waiting for Patrick's approval:\n" + "\n".join(f"- #{a['id']}: {a['summary']}" for a in pending))
        recall = self.memory.context_for(query) if self.memory else ""
        if recall:
            parts.append(recall)
        return "\n\n".join(parts)

    # ---- command cycles -----------------------------------------------------
    def cycle(self, reason: str = "scheduled check") -> str:
        """One command cycle. Never runs two at once; skips if one ran very recently."""
        if not self.lock.acquire(blocking=False):
            return "A cycle is already running."
        try:
            if time.monotonic() - self.last_cycle < self.min_gap and reason != "manual":
                return "Skipped: a cycle ran recently."
            if self.uses_gpu and self.paused is not None and self.paused.is_set():
                return "Skipped: The Forge is training on the GPU."
            if self.halt is not None and self.halt.is_set() and reason != "manual":
                return "Skipped: Wetworks is halted."
            self.last_cycle = time.monotonic()
            due = [f["id"] for f in self.followups.due()]
            self.events.busy.add("architect")
            self.events.add("architect", "start", f"Command cycle: {reason}")
            run = self.agent().run(CYCLE_GOAL.format(reason=reason), self._context("priorities leads missions"))
            self.followups.close(due)
            report = run.final or "Nothing to report."
            self.events.add("architect", "done", f"Cycle done ({len(run.steps)} steps): {report[:240]}")
            return report
        except Exception as e:
            log.exception("Command cycle failed")
            self.events.add("architect", "error", f"Command cycle failed: {type(e).__name__}")
            return f"Cycle failed: {e}"
        finally:
            self.events.busy.discard("architect")
            self.lock.release()

    def soon(self, reason: str, delay_seconds: int = 120) -> None:
        """Run a cycle shortly (debounced), e.g. after a mission finishes."""
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(delay_seconds, self.cycle, args=[reason])
        self._timer.daemon = True
        self._timer.start()

    def on_mission_done(self, mission: dict) -> None:
        if mission and mission.get("status") in ("done", "failed"):
            self.soon(f"mission #{mission['id']} {mission['status']}: {mission['title']}")

    # ---- chat ---------------------------------------------------------------
    def chat(self, history: list[dict]) -> str:
        transcript = "\n".join(f"{'Patrick' if m['sender'] == 'patrick' else 'You'}: {m['text']}" for m in history[-12:])
        latest = next((m["text"] for m in reversed(history) if m["sender"] == "patrick"), "")
        run = self.agent(steps=self.chat_steps).run(CHAT_GOAL.format(transcript=transcript), self._context(latest))
        return run.final or "I couldn't put an answer together just then. Ask me again?"

