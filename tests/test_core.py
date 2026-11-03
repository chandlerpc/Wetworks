from datetime import datetime, timezone

import pytest

from wetworks.architect import Architect
from wetworks.config import Settings
from wetworks.ledger import Ledger, cost_usd
from wetworks.llm import LLM, BudgetExceeded
from wetworks.notify import chunk


def settings(tmp_path, **kw):
    return Settings(dry_run=True, data_dir=tmp_path, discord_webhook_url="", **kw)


def test_cost():
    assert cost_usd("claude-sonnet-5-5", 1_000_000, 1_000_000) == pytest.approx(12.0)
    assert cost_usd("claude-opus-5-5", 100_000, 20_000) == pytest.approx(0.8)
    with pytest.raises(KeyError):
        cost_usd("unknown-model", 1, 1)


def test_ledger_month_to_date(tmp_path):
    led = Ledger(tmp_path / "l.db")
    led.record("architect", "claude-sonnet-5-5", 1_000_000, 0)
    led.record("intel", "claude-haiku-5-5", 0, 1_000_000)
    assert led.month_to_date() == pytest.approx(2.5)
    assert dict(led.since(datetime(2000, 1, 1, tzinfo=timezone.utc))) == {
        "architect": pytest.approx(2.0),
        "intel": pytest.approx(0.5),
    }


def test_budget_gate(tmp_path):
    s = settings(tmp_path, monthly_budget_usd=1.0)
    led = Ledger(tmp_path / "l.db")
    led.record("x", "claude-opus-5-5", 0, 100_000)  # $2
    llm = LLM(s, led)
    with pytest.raises(BudgetExceeded):
        llm.ask("x", "hi")
    reply = llm.ask("x", "hi", tier="think", essential=True)
    assert reply.model == s.model_route  # downgraded


def test_chunk():
    text = "\n".join("x" * 50 for _ in range(100))
    parts = chunk(text, limit=500)
    assert all(len(p) <= 500 for p in parts)
    assert "".join(parts) == text
    assert chunk("y" * 1200, limit=500) == ["y" * 500, "y" * 500, "y" * 200]


def test_brief_dry_run(tmp_path, capsys):
    a = Architect(settings(tmp_path))
    text = a.brief("morning")
    assert "Morning brief" in text
    assert "Spend MTD: $0.00 / $250" in text
    assert "The Architect" in capsys.readouterr().out
