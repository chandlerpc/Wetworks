"""Budget-aware model client. Every call goes through here so spend is always tracked."""

import logging
from dataclasses import dataclass

import httpx

from .config import Settings
from .ledger import SEARCH_PRICE, Ledger
from .local import Ollama, SearXNG, fetch_text

log = logging.getLogger("wetworks.llm")


class LocalModelPaused(RuntimeError):
    """The Forge has the GPU for training; the local model is unloaded until it finishes."""


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Reply:
    text: str
    model: str
    cost_usd: float
    searches: int = 0


class LLM:
    def __init__(self, settings: Settings, ledger: Ledger):
        self.s = settings
        self.ledger = ledger
        self._anthropic = None
        import threading

        self.paused = threading.Event()  # set by The Forge while it trains on the GPU
        self.ollama = Ollama(settings.ollama_url, settings.local_model, settings.local_num_ctx)
        self.searx = SearXNG(settings.searxng_url)

    @property
    def _client(self):
        """Anthropic client, created only when a paid Claude call is actually made."""
        if self.s.dry_run:
            return self._anthropic  # None unless a test injected a fake
        if self._anthropic is None:
            import anthropic

            self._anthropic = anthropic.Anthropic(api_key=self.s.anthropic_api_key)
        return self._anthropic

    @_client.setter
    def _client(self, value):
        self._anthropic = value

    # ---- helpers ---------------------------------------------------------

    def budget_left(self) -> float:
        return self.s.monthly_budget_usd - self.ledger.month_to_date()

    def _model_for(self, agent: str, tier: str, essential: bool) -> str:
        model = {"route": self.s.model_route, "work": self.s.model_work, "think": self.s.model_think}[tier]
        if self.budget_left() <= 0:
            if not essential:
                raise BudgetExceeded(f"{agent}: monthly budget of ${self.s.monthly_budget_usd:.0f} reached")
            model = self.s.model_route
        return model

    @property
    def grok_available(self) -> bool:
        return bool(self.s.xai_api_key) and not self.s.dry_run

    # ---- free local calls ------------------------------------------------

    def ask_local(self, agent: str, prompt: str, *, system: str = "", json_mode: bool = False,
                  max_tokens: int = 1024) -> Reply:
        """Ask the local Ollama model. Free; logged at $0 so usage still shows in the ledger."""
        if self.paused.is_set():
            raise LocalModelPaused("The Forge is training on the GPU; the local model is back when it finishes")
        model = f"local:{self.ollama.model}"
        if self.s.dry_run:
            stub = f"[dry-run {model}] {prompt[:200]}"
            if json_mode:  # one shape every JSON caller understands: no findings, no mission
                import json as _json

                stub = _json.dumps({"reply": stub, "done": stub, "findings": [], "mission": None})
            return Reply(text=stub, model=model, cost_usd=0.0)
        text, tin, tout = self.ollama.chat(prompt, system=system, json_mode=json_mode, max_tokens=max_tokens)
        self.ledger.record_cost(agent, model, tin, tout, 0.0)
        return Reply(text=text, model=model, cost_usd=0.0)

    def web_search_local(self, query: str, time_range: str = "week", limit: int = 8) -> list[dict]:
        if self.s.dry_run:
            return []
        return self.searx.search(query, time_range=time_range, limit=limit)

    def read_page_local(self, url: str, max_chars: int = 3000) -> str:
        if self.s.dry_run:
            return ""
        return fetch_text(url, max_chars=max_chars)

    # ---- plain calls -----------------------------------------------------

    def ask(
        self,
        agent: str,
        prompt: str,
        *,
        tier: str = "work",
        system: str = "",
        max_tokens: int = 1024,
        essential: bool = False,
    ) -> Reply:
        """Ask a Claude model. tier = route | work | think.

        Over budget, non-essential calls are refused and essential ones drop to the route tier.
        """
        model = self._model_for(agent, tier, essential)
        if self._client is None:  # dry run
            return Reply(text=f"[dry-run {model}] {prompt[:200]}", model=model, cost_usd=0.0)

        kwargs = {"system": system} if system else {}
        msg = self._client.messages.create(
            model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}], **kwargs
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        cost = self.ledger.record(agent, model, msg.usage.input_tokens, msg.usage.output_tokens)
        return Reply(text=text, model=model, cost_usd=cost)

    # ---- web search ------------------------------------------------------

    def search(
        self,
        agent: str,
        prompt: str,
        *,
        tier: str = "work",
        system: str = "",
        max_searches: int = 3,
        max_tokens: int = 2048,
        prefer_grok: bool = False,
    ) -> Reply:
        """Answer a prompt using live web search. Uses Grok when preferred and keyed, else Claude."""
        if self.budget_left() <= 0:
            raise BudgetExceeded(f"{agent}: monthly budget of ${self.s.monthly_budget_usd:.0f} reached")
        if self._client is None:  # dry run
            return Reply(text="[]", model="dry-run", cost_usd=0.0)
        if prefer_grok and self.grok_available:
            try:
                return self._search_grok(agent, prompt, system=system)
            except Exception as e:
                log.warning("%s: Grok search failed (%s); falling back to Claude", agent, e)
        return self._search_claude(agent, prompt, tier=tier, system=system,
                                   max_searches=max_searches, max_tokens=max_tokens)

    def _search_claude(self, agent, prompt, *, tier, system, max_searches, max_tokens) -> Reply:
        model = self._model_for(agent, tier, essential=False)
        tools = [{"type": self.s.claude_web_search_tool, "name": "web_search", "max_uses": max_searches}]
        messages = [{"role": "user", "content": prompt}]
        kwargs = {"system": system} if system else {}
        tin = tout = searches = 0
        text = ""
        for _ in range(4):  # server-side search can pause long turns; resume a few times at most
            msg = self._client.messages.create(
                model=model, max_tokens=max_tokens, tools=tools, messages=messages, **kwargs
            )
            tin += msg.usage.input_tokens
            tout += msg.usage.output_tokens
            stu = getattr(msg.usage, "server_tool_use", None)
            searches += getattr(stu, "web_search_requests", 0) or 0
            text += "".join(b.text for b in msg.content if b.type == "text")
            if msg.stop_reason != "pause_turn":
                break
            messages = messages + [{"role": "assistant", "content": msg.content}]
        cost = self.ledger.record(agent, model, tin, tout, extra_usd=searches * SEARCH_PRICE["claude"])
        return Reply(text=text, model=model, cost_usd=cost, searches=searches)

    def _search_grok(self, agent, prompt, *, system) -> Reply:
        body = {
            "model": self.s.grok_model,
            "input": ([{"role": "system", "content": system}] if system else [])
            + [{"role": "user", "content": prompt}],
            "tools": [{"type": "web_search"}, {"type": "x_search"}],
        }
        r = httpx.post(
            "https://api.x.ai/v1/responses",
            headers={"Authorization": f"Bearer {self.s.xai_api_key}"},
            json=body,
            timeout=180,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(data["error"])
        text = "".join(
            part.get("text", "")
            for item in data.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        )
        searches = sum(1 for item in data.get("output", []) if item.get("type", "").endswith("search_call"))
        usage = data.get("usage", {})
        tin, tout = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        cost = (tin * self.s.grok_price_in + tout * self.s.grok_price_out) / 1_000_000
        cost += searches * SEARCH_PRICE["grok"]
        self.ledger.record_cost(agent, self.s.grok_model, tin, tout, cost)
        return Reply(text=text, model=self.s.grok_model, cost_usd=cost, searches=searches)
