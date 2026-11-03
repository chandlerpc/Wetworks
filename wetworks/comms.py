"""Two-way comms with The Architect and clAIr, used by the console (and later clAIr's bridge).

Replies are generated in a background thread so the phone never waits on a slow model;
the console polls for new messages.
"""

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
        "and never invent facts. If something needs real work, say you'll "
        "pass it to The Architect."
    ),
}

HISTORY_TURNS = 10


class Comms:
    def __init__(self, path: Path, llm, events, context_fn):
        """context_fn() returns a text block of current status and findings for the model."""
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
                "Reply to Patrick's latest message."
            )
            try:
                if self.llm.s.provider == "local":
                    reply = self.llm.ask_local(f"comms:{channel}", prompt, system=PERSONAS[channel], max_tokens=600)
                else:
                    reply = self.llm.ask(f"comms:{channel}", prompt, tier="work", system=PERSONAS[channel], max_tokens=600)
                text = reply.text.strip() or "I came back empty on that one. Try asking again, or rephrase it."
            except Exception as e:
                text = f"Comms down: {type(e).__name__}. The local model may still be loading; try again in a minute."
                self.events.add(channel, "error", f"Reply failed: {e}")
            self._insert(channel, channel, text)
