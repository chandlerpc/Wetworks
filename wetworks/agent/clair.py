"""C.L.a.i.R as an agent.

She's Patrick's assistant and the comms handler: she answers him (typed or by voice), looks
things up, opens missions, keeps notes, and hands strategy to The Architect so he stays on task.
Her brain is Gemini via her Cloudflare Worker, with the local model as backup.
"""

from .runtime import Agent

PERSONA = (
    "You are C.L.a.i.R, Patrick's personal assistant and the comms handler for Wetworks, his "
    "multi-agent ops center. You speak with him directly, often by voice, so keep replies short, warm "
    "and clear: a few sentences, no markdown, no lists unless he asks. You have tools: use them to "
    "check facts (missions, leads, findings, memory, the web) instead of guessing, and to act. For real "
    "work (research, drafting, planning, code changes) start a mission rather than doing it in chat. "
    "Pass strategy questions and anything that needs coordinating several missions to The Architect "
    "with tell_architect, so he stays focused. Remember durable things Patrick tells you. Never send "
    "anything outside Wetworks yourself; those actions need Patrick's approval. Quote numbers exactly "
    "as tools or status give them. For questions about how Wetworks works, check read_handbook."
)

GOAL = """Patrick is talking to you. Conversation so far:
{transcript}

Reply to his latest message. Use tools if you need to look something up or act. Your done text is
exactly what he'll hear or read, so write it as you'd say it."""


class ClairAgent:
    def __init__(self, *, toolbox, ask, approvals, events, context_fn, memory, max_steps: int = 5):
        self.toolbox = toolbox
        self.ask = ask
        self.approvals = approvals
        self.events = events
        self.context_fn = context_fn
        self.memory = memory
        self.max_steps = max_steps

    def chat(self, history: list[dict]) -> str:
        transcript = "\n".join(f"{'Patrick' if m['sender'] == 'patrick' else 'You'}: {m['text']}" for m in history[-12:])
        latest = next((m["text"] for m in reversed(history) if m["sender"] == "patrick"), "")
        context = [self.context_fn()]
        pending = self.approvals.pending() if self.approvals else []
        if pending:
            context.append("Waiting for Patrick's approval:\n" + "\n".join(f"- #{a['id']}: {a['summary']}" for a in pending))
        recall = self.memory.context_for(latest) if self.memory else ""
        if recall:
            context.append(recall)
        agent = Agent("clair", PERSONA, self.toolbox, self.ask, approvals=self.approvals, events=self.events,
                      max_steps=self.max_steps)
        run = agent.run(GOAL.format(transcript=transcript), "\n\n".join(context))
        return run.final or "Sorry, I lost my thread there. Say that again?"
