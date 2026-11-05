"""The model router: role order, fallbacks, per-vendor caps, cross-vendor review."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from wetworks.config import Settings
from wetworks.ledger import Ledger
from wetworks.models import NoModel, Router, review

CFG = Path("config/models.toml")


class FakeLocal:
    def __init__(self):
        self.calls = 0

    def ask_local(self, agent, prompt, system="", json_mode=False, max_tokens=1024):
        self.calls += 1
        return SimpleNamespace(text="local answer", model="gemma4:12b")


def make(tmp_path, **keys):
    s = Settings(dry_run=False, data_dir=tmp_path, config_dir="config", monthly_budget_usd=100, **keys)
    r = Router(s, Ledger(tmp_path / "ledger.db"), FakeLocal(), CFG)
    r.calls = []

    def transport(vendor, model, system, prompt, max_tokens, json_mode):
        r.calls.append(model)
        if model in getattr(r, "broken", ()):
            raise RuntimeError("down")
        return (r.reply if hasattr(r, "reply") else f"from {model}"), 1000, 500
    r.transport = transport
    return r


ALL = dict(anthropic_api_key="a", openai_api_key="o", xai_api_key="x", gemini_api_key="g")


def test_first_model_in_role_with_a_key(tmp_path):
    r = make(tmp_path, **ALL)
    out = r.ask("plan", "t", "hi")
    assert out.model == "claude-opus-5-5" and out.vendor == "anthropic" and out.cost_usd > 0
    assert r.last_brain() == "claude"
    assert r.ledger.month_to_date() == pytest.approx(out.cost_usd)


def test_skips_vendors_without_keys(tmp_path):
    r = make(tmp_path, gemini_api_key="g")
    assert r.pick("plan") == "gemini-4-argon"
    assert r.ask("reason", "t", "hi").model == "gemini-3.8-flash"


def test_falls_back_on_errors_then_to_local(tmp_path):
    r = make(tmp_path, anthropic_api_key="a")
    r.broken = {"claude-sonnet-5-5"}
    out = r.ask("reason", "t", "hi")
    assert out.vendor == "local" and r.llm.calls == 1 and r.last_brain() == "local"


def test_vendor_cap_moves_work_to_the_next_vendor(tmp_path):
    r = make(tmp_path, **ALL)
    r.ledger.record_cost("t", "claude-opus-5-5", 0, 0, 55.0)  # Anthropic's monthly cap
    assert r.pick("plan") == "gpt-5.5"
    assert r.status()["vendors"]["anthropic"]["spent"] == 55.0


def test_total_budget_stops_all_paid_calls(tmp_path):
    r = make(tmp_path, **ALL)
    r.ledger.record_cost("t", "local-thing", 0, 0, 100.0)
    assert r.pick("plan") is None
    assert r.ask("plan", "t", "hi").vendor == "local"


def test_review_never_uses_the_author_vendor_and_never_local(tmp_path):
    r = make(tmp_path, **ALL)
    r.reply = '{"verdict": "fix", "issues": ["claims a result"], "suggestion": "cut it"}'
    out = review(r, "an email", "draft", author_vendor="openai")
    assert out["reviewer"] == "claude-sonnet-5-5" and out["verdict"] == "fix" and out["issues"] == ["claims a result"]
    r2 = make(tmp_path / "b", openai_api_key="o")
    with pytest.raises(NoModel):
        review(r2, "an email", "draft", author_vendor="openai")
    assert r2.llm.calls == 0


def test_review_with_junk_reply_defaults_to_fix(tmp_path):
    r = make(tmp_path, **ALL)
    r.reply = "not json"
    assert review(r, "a post", "text")["verdict"] == "fix"
