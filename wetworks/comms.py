"""Two-way comms with The Architect and clAIr, used by the console (and later clAIr's bridge).

Replies are generated in a background thread so the phone never waits on a slow model;
the console polls for new messages.
"""

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

PERSONAS = {
    "architect": (
        "You are The Architect, overseer of Wetworks, Patrick's multi-agent ops center. "
        "You plan, delegate to cells and report. Speak like a calm ops-center handler: short, "
        "precise, no filler. Use only the status and findings you're given and quote numbers "
        "exactly as written there (spend, budget, counts); if you don't know "
        "something, say so and say which cell could find out. Mission priorities: make money, "
        "make the day job easier, manage finances, free up time, build games, create content."
    ),
    "clair": (
        "You are clAIr, Patrick's assistant and the comms handler for Wetworks. You keep him "
        "informed and keep The Architect focused by handling requests and updates. Warm, quick "
        "and clear; a few sentences at most unless he asks for more. Use only the status and "
        "findings you're given, quote numbers exactly as written there (spend, budget, counts), "
        "and never invent facts. If something needs real work, open a mission so The Architect's "
        "Splinter team handles it, and tell Patrick you've passed it on."
    ),
}

HISTORY_TURNS = 10

REPLY_FORMAT = """Reply with ONLY this JSON object:
{"reply": "what you say to Patrick",
 "mission": null}
If Patrick is asking for real work to be done (research something, find options, draft or write
something, make a plan, compare things), open a mission instead of doing it in chat:
{"reply": "short confirmation", "mission": {"title": "short title, under 8 words",
 "request": "the full task, in enough detail for an operative who hasn't seen this chat"}}
Only open a mission for a new request; don't reopen one that's already queued or running.
Small talk, questions about status, and quick answers you can give from the status above are not missions."""


def parse_reply(text: str) -> tuple[str, dict | None]:
    """Split a model reply into (what to say, optional mission). Falls back to plain text."""
    text = (text or "").strip()
    candidates = [text]
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            data = json.loads(c)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and "reply" in data:
            mission = data.get("mission")
            if not (isinstance(mission, dict) and str(mission.get("request", "")).strip()):
                mission = None
            return str(data.get("reply") or "").strip(), mission
    return text, None


class Comms:
    def __init__(self, path: Path, llm, events, context_fn, open_mission=None):
        """context_fn() returns a text block of current status and findings for the model.

        open_mission(title, request, source) queues a mission and returns it (dict with "id").
        """
        self.open_mission = open_mission
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                channel TEXT NOT NULL,
                sender TEXT NOT NULL,
                text TEXT NOT NULL
            )"""
        )
        self.db.commit()
        self.llm = llm
        self.events = events
        self.context_fn = context_fn

    def _insert(self, channel: str, sender: str, text: str) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO messages (ts, channel, sender, text) VALUES (?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), channel, sender, text),
            )
            self.db.commit()
            return cur.lastrowid

    def messages(self, channel: str, after_id: int = 0, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id, ts, channel, sender, text FROM messages WHERE channel = ? AND id > ? "
                "ORDER BY id DESC LIMIT ?",
                (channel, after_id, limit),
            ).fetchall()[::-1]
        return [dict(zip(("id", "ts", "channel", "sender", "text"), r)) for r in rows]

    def post(self, channel: str, text: str, wait: bool = False) -> int:
        """Store Patrick's message and start the reply. Returns the message id."""
        if channel not in PERSONAS:
            raise KeyError(channel)
        text = text.strip()[:4000]
        if not text:
            raise ValueError("empty message")
        mid = self._insert(channel, "patrick", text)
        t = threading.Thread(target=self._reply, args=(channel,), daemon=True)
        t.start()
        if wait:
            t.join()
        return mid

    def _reply(self, channel: str) -> None:
        with self.events.working(channel):
            history = self.messages(channel, limit=HISTORY_TURNS * 2)
            transcript = "\n".join(
                f"{'Patrick' if m['sender'] == 'patrick' else m['sender']}: {m['text']}" for m in history
            )
            prompt = (
                f"Current Wetworks status:\n{self.context_fn()}\n\n"
                f"Conversation so far:\n{transcript}\n\n"
                "Reply to Patrick's latest message.\n\n" + (REPLY_FORMAT if self.open_mission else "")
            )
            json_mode = self.open_mission is not None
            try:
                if self.llm.s.provider == "local":
                    reply = self.llm.ask_local(f"comms:{channel}", prompt, system=PERSONAS[channel],
                                               json_mode=json_mode, max_tokens=700)
                else:
                    reply = self.llm.ask(f"comms:{channel}", prompt, tier="work", system=PERSONAS[channel], max_tokens=700)
                text, mission = parse_reply(reply.text) if json_mode else (reply.text.strip(), None)
                if mission and self.open_mission:
                    m = self.open_mission(str(mission.get("title", "")), str(mission["request"]), channel)
                    text = (text or "On it.") + f"\n\nMission #{m['id']} opened: {m['title']}. Track it in Missions."
                text = text or "I came back empty on that one. Try asking again, or rephrase it."
            except Exception as e:
                text = f"Comms down: {type(e).__name__}. The local model may still be loading; try again in a minute."
                self.events.add(channel, "error", f"Reply failed: {e}")
            self._insert(channel, channel, text)

    def mission_report(self, mission: dict) -> None:
        """Post a finished mission back into the channel that opened it."""
        channel = mission.get("source")
        if channel not in PERSONAS:
            return
        status = mission["status"]
        if status == "done":
            first = mission["result"].strip().split("\n\n")[0][:400]
            text = f"Mission #{mission['id']} is done: {mission['title']}.\n\n{first}\n\nFull write-up is in Missions."
        elif status == "failed":
            text = f"Mission #{mission['id']} hit a problem: {mission['title']}. Details are in Missions."
        else:
            text = f"Mission #{mission['id']} was {status}."
        self._insert(channel, channel, text)
