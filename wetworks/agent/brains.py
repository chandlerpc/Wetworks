"""How agents think: one function, ask(system, prompt, max_tokens) -> text."""

from typing import Callable

Ask = Callable[[str, str, int], str]


def make_ask(settings, llm, agent: str = "agent", brain: str | None = None) -> Ask:
    """The default brain: the free local model, or Claude when the brain is claude-api."""
    def ask(system: str, prompt: str, max_tokens: int) -> str:
        if (brain or settings.agent_brain) == "claude-api":
            return llm.ask(agent, prompt, tier="think", system=system, max_tokens=max_tokens).text
        return llm.ask_local(agent, prompt, system=system, json_mode=True, max_tokens=max_tokens).text
    return ask


def clair_ask(bridge, fallback: Ask, on_mode: Callable[[str, str], None]) -> Ask:
    """C.L.a.i.R thinks on Gemini through her Cloudflare Worker; if that fails, the local model
    takes over for that step so she never goes silent. on_mode(mode, why) reports which one answered."""
    def ask(system: str, prompt: str, max_tokens: int) -> str:
        if bridge is not None and bridge.configured:
            try:
                text = bridge.step(system, prompt, max_tokens)
                on_mode("cloudflare", "")
                return text
            except Exception as e:
                on_mode("fallback", str(e))
        else:
            on_mode("local", "")
        return fallback(system, prompt, max_tokens)
    return ask
