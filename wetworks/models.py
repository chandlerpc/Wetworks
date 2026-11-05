"""The model router: every paid model call that isn't private goes through here.

config/models.toml lists the models, their vendors and prices, which models serve each job (role), and a
monthly cap per vendor. ask(role, ...) uses the first model in the role that has a key and budget left,
falls back down the list on errors or caps, and finally to the free local model. Every call is priced in
the ledger. Private rooms never call this.
"""

import json
import logging
import threading
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger("wetworks.models")

BRAIN = {"anthropic": "claude", "openai": "openai", "xai": "grok", "google": "gemini", "local": "local"}


class NoModel(RuntimeError):
    """No model could serve a strict role (no keys, caps reached, or every call failed)."""


@dataclass
class Routed:
    text: str
    model: str
    vendor: str
    cost_usd: float
    searches: int = 0


class Router:
    def __init__(self, settings, ledger, llm, config_path: Path):
        self.s, self.ledger, self.llm, self.path = settings, ledger, llm, config_path
        self.last = threading.local()  # last vendor used on this thread (for decision traces)
        self.transport = None          # tests inject a fake: fn(vendor, model, system, prompt, max_tokens, json_mode) -> (text, in, out)

    # ---- config --------------------------------------------------------------------
    def config(self) -> dict:
        try:
            with open(self.path, "rb") as f:
                return tomllib.load(f)
        except OSError:
            return {}

    def keys(self) -> dict:
        s = self.s
        return {"anthropic": s.anthropic_api_key, "openai": s.openai_api_key, "xai": s.xai_api_key, "google": s.gemini_api_key}

    def vendor_of(self, model: str) -> str:
        return (self.config().get("models", {}).get(model) or {}).get("vendor", "")

    def price(self, model: str) -> tuple[float, float]:
        p = (self.config().get("models", {}).get(model) or {}).get("price")
        if p:
            return float(p[0]), float(p[1])
        from .ledger import PRICES

        return PRICES.get(model, (0.0, 0.0))

    def spend_by_vendor(self) -> dict[str, float]:
        start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        rows = self.ledger.db.execute("SELECT model, COALESCE(SUM(cost_usd), 0) FROM calls WHERE ts >= ? GROUP BY model",
                                      (start,)).fetchall()
        out: dict[str, float] = {}
        for model, total in rows:
            v = self.vendor_of(model) or ("anthropic" if model.startswith("claude") else "")
            if v:
                out[v] = out.get(v, 0.0) + float(total)
        return out

    def status(self) -> dict:
        """For the console: per vendor spend vs cap and whether it has a key; which model each role would use."""
        cfg, spend, keys = self.config(), self.spend_by_vendor(), self.keys()
        vendors = {v: {"spent": round(spend.get(v, 0.0), 2), "cap": float(c), "key": bool(keys.get(v))}
                   for v, c in cfg.get("budget", {}).items()}
        roles = {r: (self.pick(r) or ("local" if not cfg.get("strict", {}).get(r) else None)) for r in cfg.get("roles", {})}
        return {"vendors": vendors, "roles": roles, "total_budget": self.s.monthly_budget_usd,
                "total_spent": round(self.ledger.month_to_date(), 2)}

    # ---- choosing --------------------------------------------------------------------
    def usable(self, model: str, exclude_vendor: str = "") -> bool:
        v = self.vendor_of(model)
        if not v or v == exclude_vendor or not self.keys().get(v):
            return False
        cap = self.config().get("budget", {}).get(v)
        if cap is not None and self.spend_by_vendor().get(v, 0.0) >= float(cap):
            return False
        return self.ledger.month_to_date() < self.s.monthly_budget_usd

    def pick(self, role: str, exclude_vendor: str = "") -> str | None:
        return next((m for m in self.config().get("roles", {}).get(role, []) if self.usable(m, exclude_vendor)), None)

    # ---- calling -----------------------------------------------------------------------
    def ask(self, role: str, agent: str, prompt: str, *, system: str = "", max_tokens: int = 1024,
            json_mode: bool = False, exclude_vendor: str = "") -> Routed:
        cfg = self.config()
        tried = []
        for model in cfg.get("roles", {}).get(role, []):
            if not self.usable(model, exclude_vendor):
                continue
            vendor = self.vendor_of(model)
            try:
                text, tin, tout = self._call(vendor, model, system, prompt, max_tokens, json_mode)
            except Exception as e:  # try the next model in the role
                log.warning("%s: %s (%s) failed: %s", agent, model, vendor, str(e)[:200])
                tried.append(f"{model}: {type(e).__name__}")
                continue
            pin, pout = self.price(model)
            cost = (tin * pin + tout * pout) / 1_000_000
            self.ledger.record_cost(agent, model, tin, tout, cost)
            self.last.vendor = vendor
            return Routed(text=text, model=model, vendor=vendor, cost_usd=cost)
        if cfg.get("strict", {}).get(role):
            raise NoModel(f"No model available for {role!r}" + (f" ({'; '.join(tried)})" if tried else " (no keys or budget)"))
        r = self.llm.ask_local(agent, prompt, system=system, json_mode=json_mode, max_tokens=max_tokens)
        self.last.vendor = "local"
        return Routed(text=r.text, model=r.model, vendor="local", cost_usd=0.0)

    def last_brain(self) -> str:
        return BRAIN.get(getattr(self.last, "vendor", "local"), "local")

    def _call(self, vendor, model, system, prompt, max_tokens, json_mode) -> tuple[str, int, int]:
        if self.transport is not None:
            return self.transport(vendor, model, system, prompt, max_tokens, json_mode)
        if self.s.dry_run:
            return f"[dry-run {model}] {prompt[:200]}", 0, 0
        keys = self.keys()
        if vendor == "anthropic":
            client = self.llm._client
            kwargs = {"system": system} if system else {}
            msg = client.messages.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}], **kwargs)
            return "".join(b.text for b in msg.content if b.type == "text"), msg.usage.input_tokens, msg.usage.output_tokens
        if vendor in ("openai", "xai"):
            url = "https://api.openai.com/v1/chat/completions" if vendor == "openai" else "https://api.x.ai/v1/chat/completions"
            body = {"model": model, "messages": ([{"role": "system", "content": system}] if system else [])
                    + [{"role": "user", "content": prompt}]}
            body["max_completion_tokens" if vendor == "openai" else "max_tokens"] = max_tokens
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            r = httpx.post(url, headers={"Authorization": f"Bearer {keys[vendor]}"}, json=body, timeout=180)
            r.raise_for_status()
            d = r.json()
            u = d.get("usage", {})
            return (d["choices"][0]["message"].get("content") or "").strip(), int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
        if vendor == "google":
            body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": max_tokens, **({"responseMimeType": "application/json"} if json_mode else {})}}
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                           headers={"x-goog-api-key": keys["google"]}, json=body, timeout=180)
            r.raise_for_status()
            d = r.json()
            parts = ((d.get("candidates") or [{}])[0].get("content") or {}).get("parts", [])
            u = d.get("usageMetadata", {})
            return "".join(p.get("text", "") for p in parts).strip(), int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0))
        raise ValueError(f"Unknown vendor {vendor!r}")


REVIEW = """You are giving a second opinion on work another AI model did for Patrick Caldwell (AI and software
developer, Sanford FL, runs CodeVoyage). Be specific and brief. Judge it against what it's for.

What it is: {what}
{context}
The work:
---
{work}
---

Reply with ONLY JSON:
{{"verdict": "ok" | "fix" | "stop",
  "issues": ["specific problems: wrong facts, invented claims, tone, risks, missing pieces"],
  "suggestion": "the single most useful change, or empty if ok"}}
"ok" = good to use as is; "fix" = usable after the suggested change; "stop" = don't use it."""


def review(router: Router, what: str, work: str, author_vendor: str = "local", context: str = "", agent: str = "review") -> dict:
    """Cross-vendor second opinion. Never reviewed by the vendor that wrote it."""
    r = router.ask("review", agent, REVIEW.format(what=what, context=f"Context: {context}\n" if context else "", work=work[:12000]),
                   max_tokens=700, json_mode=True, exclude_vendor=author_vendor)
    try:
        from .code.brain import parse_json_obj

        data = parse_json_obj(r.text) or {}
    except Exception:
        data = {}
    verdict = data.get("verdict") if data.get("verdict") in ("ok", "fix", "stop") else "fix"
    return {"verdict": verdict, "issues": [str(i)[:300] for i in (data.get("issues") or [])][:6],
            "suggestion": str(data.get("suggestion") or "")[:600], "reviewer": r.model, "cost": round(r.cost_usd, 4)}
