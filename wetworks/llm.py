"""Budget-aware model client. Every call goes through here so spend is always tracked."""

from dataclasses import dataclass

from .config import Settings
from .ledger import Ledger


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Reply:
    text: str
    model: str
    cost_usd: float


class LLM:
    def __init__(self, settings: Settings, ledger: Ledger):
        self.s = settings
        self.ledger = ledger
        self._client = None
        if not settings.dry_run:
            import anthropic

            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def budget_left(self) -> float:
        return self.s.monthly_budget_usd - self.ledger.month_to_date()

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
        """Ask a model. tier = route | work | think.

        Over budget, non-essential calls are refused and essential ones drop to the route tier.
        """
        model = {"route": self.s.model_route, "work": self.s.model_work, "think": self.s.model_think}[tier]
        if self.budget_left() <= 0:
            if not essential:
                raise BudgetExceeded(f"{agent}: monthly budget of ${self.s.monthly_budget_usd:.0f} reached")
            model = self.s.model_route

        if self._client is None:  # dry run
            text = f"[dry-run {model}] {prompt[:200]}"
            return Reply(text=text, model=model, cost_usd=0.0)

        msg = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system or None,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in msg.content if block.type == "text")
        cost = self.ledger.record(agent, model, msg.usage.input_tokens, msg.usage.output_tokens)
        return Reply(text=text, model=model, cost_usd=cost)
