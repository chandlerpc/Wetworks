from types import SimpleNamespace

import pytest

from wetworks.architect import Architect, format_findings
from wetworks.cells.base import CellContext
from wetworks.cells.intel import Intel, parse_findings
from wetworks.config import Settings
from wetworks.findings import Finding, FindingsStore
from wetworks.ledger import Ledger
from wetworks.llm import LLM, Reply
from wetworks.operators import offset_hhmm

TOPICS = """
[intel]
trial_start = "2026-11-03"
[[topics]]
name = "AI"
brief = "models"
max_items = 2
[[topics]]
name = "Off"
enabled = false
brief = "x"
"""


def ctx(tmp_path, llm=None, provider="claude"):
    s = Settings(dry_run=True, data_dir=tmp_path, discord_webhook_url="", config_dir=tmp_path, provider=provider)
    (tmp_path / "intel.toml").write_text(TOPICS)
    led = Ledger(tmp_path / "l.db")
    return CellContext(settings=s, llm=llm or LLM(s, led), findings=FindingsStore(tmp_path / "f.db"))


class FakeLLM:
    def __init__(self, text, results=None):
        self.text, self.calls, self.results = text, [], results or []

    def search(self, agent, prompt, **kw):
        self.calls.append((agent, prompt, kw))
        return Reply(text=self.text, model="fake", cost_usd=0.02, searches=2)

    def web_search_local(self, query, **kw):
        self.calls.append(("searx", query, kw))
        return self.results

    def ask_local(self, agent, prompt, **kw):
        self.calls.append((agent, prompt, kw))
        return Reply(text=self.text, model="local:fake", cost_usd=0.0)


def test_local_intel_is_free_and_maps_index_to_real_url(tmp_path):
    results = [
        {"title": "Model X launches", "url": "https://real/1", "content": "details"},
        {"title": "Old news", "url": "https://real/2", "content": "meh"},
    ]
    reply = ('{"findings": [{"index": 0, "title": "Model X", "summary": "big", "importance": 4},'
             ' {"index": 7, "title": "Hallucinated", "summary": "x", "importance": 5}]}')
    fake = FakeLLM(reply, results)
    intel = Intel(ctx(tmp_path, fake, provider="local"))
    found = intel.run()
    assert [(f.title, f.url) for f in found] == [("Model X", "https://real/1")]  # bad index dropped
    assert not any(c[0].startswith("intel") and "prefer_grok" in c[2] for c in fake.calls)  # no paid search
    assert fake.calls[0] == ("searx", "AI", {"time_range": "week", "limit": 8})  # falls back to topic name


def test_default_provider_is_free(tmp_path):
    s = Settings(dry_run=True, data_dir=tmp_path)
    assert s.provider == "local"
    a = Architect(s)
    assert "local:" in a.brief("morning")
    assert a.ledger.month_to_date() == 0


def test_parse_findings_variants():
    raw = '[{"title":"A","url":"u1","importance":4}]'
    assert parse_findings(raw)[0]["title"] == "A"
    assert parse_findings(f"Here you go:\n```json\n{raw}\n```")[0]["url"] == "u1"
    assert parse_findings(f"Results: {raw} done")[0]["importance"] == 4
    assert parse_findings("nothing found") == []
    assert parse_findings('[{"no_title": 1}]') == []


def test_intel_stores_dedups_and_caps(tmp_path):
    reply = (
        '[{"title":"One","summary":"s","url":"https://a","importance":9},'
        '{"title":"Two","summary":"s","url":"https://b","importance":"x"},'
        '{"title":"Three","summary":"s","url":"https://c","importance":2}]'
    )
    fake = FakeLLM(reply)
    intel = Intel(ctx(tmp_path, fake))
    assert [t["name"] for t in intel.topics] == ["AI"]
    assert intel.in_trial(today=__import__("datetime").date(2026, 11, 10))

    found = intel.run()
    assert [f.title for f in found] == ["One", "Two"]  # capped at max_items=2
    assert found[0].importance == 5 and found[1].importance == 3  # clamped / defaulted
    assert fake.calls[0][2]["prefer_grok"] is False  # provider=claude, so no Grok
    assert intel.run() == []  # same URLs again → deduped
    assert "- One" in fake.calls[1][1]  # already-seen titles are passed back to the model


def test_brief_includes_and_marks_findings(tmp_path):
    s = Settings(dry_run=True, data_dir=tmp_path, discord_webhook_url="")
    a = Architect(s)
    a.findings.add(Finding(cell="intel", topic="AI", title="Big release", summary="matters", url="https://x", importance=5))
    a.findings.add(Finding(cell="intel", topic="AI", title="Trivia", summary="meh", url="https://y", importance=1))
    text = a.brief("morning")
    assert "**Intel**" in text and "🔴 **Big release**" in text and "Trivia" not in text
    assert "**Intel**" not in a.brief("evening")  # already reported


def test_grok_search_parses_and_costs(tmp_path, monkeypatch):
    s = Settings(dry_run=True, data_dir=tmp_path, xai_api_key="k", grok_price_in=3.0, grok_price_out=15.0)
    led = Ledger(tmp_path / "l.db")
    llm = LLM(s, led)
    payload = {
        "output": [
            {"type": "web_search_call"},
            {"type": "x_search_call"},
            {"type": "message", "content": [{"type": "output_text", "text": '[{"title":"G"}]'}]},
        ],
        "usage": {"input_tokens": 100_000, "output_tokens": 10_000},
    }
    resp = SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
    monkeypatch.setattr("wetworks.llm.httpx.post", lambda *a, **k: resp)
    r = llm._search_grok("intel:AI", "q", system="")
    # 100k*$3/M + 10k*$15/M = $0.45, plus 2 searches * $0.01
    assert r.text == '[{"title":"G"}]' and r.searches == 2
    assert r.cost_usd == pytest.approx(0.47)


def test_format_findings_groups_by_topic():
    out = format_findings([
        Finding(cell="i", topic="A", title="t1", summary="s", importance=4),
        Finding(cell="i", topic="B", title="t2", summary="s", url="https://z"),
    ])
    assert "__A__" in out and "🟠 **t1**" in out and "<https://z>" in out


def test_offset_wraps_midnight():
    assert offset_hhmm("07:00", 20) == (6, 40)
    assert offset_hhmm("00:10", 20) == (23, 50)


def test_claude_search_records_search_cost(tmp_path):
    s = Settings(dry_run=True, data_dir=tmp_path)
    led = Ledger(tmp_path / "l.db")
    llm = LLM(s, led)
    msg = SimpleNamespace(
        content=[SimpleNamespace(type="server_tool_use"), SimpleNamespace(type="text", text="[]")],
        usage=SimpleNamespace(input_tokens=10_000, output_tokens=1_000,
                              server_tool_use=SimpleNamespace(web_search_requests=3)),
        stop_reason="end_turn",
    )
    llm._client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: msg))
    r = llm.search("intel:AI", "q")
    # sonnet: 10k*$2/M + 1k*$10/M = $0.03, plus 3 searches * $0.01
    assert r.cost_usd == pytest.approx(0.06) and r.searches == 3
    assert led.month_to_date() == pytest.approx(0.06)
