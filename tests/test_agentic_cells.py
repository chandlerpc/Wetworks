import json
from pathlib import Path
from types import SimpleNamespace

from wetworks.agent.brains import clair_ask
from wetworks.agent.clair import ClairAgent
from wetworks.agent.state import Approvals, Followups
from wetworks.agent.toolkit import build_toolbox
from wetworks.bridge import BridgeError
from wetworks.cells.base import CellContext
from wetworks.cells.intel import Intel
from wetworks.cells.revenue import Revenue
from wetworks.cells.splinter import Splinter
from wetworks.config import Settings
from wetworks.events import EventLog
from wetworks.findings import FindingsStore
from wetworks.leads import LeadStore
from wetworks.memory import Memory
from wetworks.missions import MissionStore
from wetworks.operators import SplinterOperator

from .test_agents import Script, stores

ROOT = Path(__file__).parent.parent


class Web:
    """Fake free search: every query returns the same two pages."""

    def __init__(self, results=None):
        self.queries, self.reads = [], []
        self.results = results or [
            {"title": "Model X released", "url": "https://news.example/x", "content": "A new open model."},
            {"title": "Sunrise Spa hiring receptionist", "url": "https://sunrisespa.example/jobs", "content": "Sanford spa."},
        ]

    def web_search_local(self, q, **kw):
        self.queries.append(q)
        return list(self.results)

    def read_page_local(self, url, **kw):
        self.reads.append(url)
        return f"Full text of {url}"


def ctx(tmp_path, ask, web=None):
    return CellContext(settings=Settings(dry_run=True, data_dir=tmp_path, config_dir=ROOT / "config"),
                       llm=web or Web(), findings=FindingsStore(tmp_path / "f.db"), events=EventLog(tmp_path / "e.db"),
                       memory=Memory(tmp_path / "m.db"), ask=ask)


# ---------------- C.L.a.i.R ----------------

class FakeBridge:
    configured = True

    def __init__(self, answers=None, error=None):
        self.answers, self.error, self.calls = list(answers or []), error, 0

    def step(self, system, prompt, max_tokens=900):
        self.calls += 1
        if self.error:
            raise self.error
        return self.answers.pop(0)


def test_clair_thinks_on_gemini_and_uses_tools(tmp_path):
    deps, opened, _ = stores(tmp_path)
    modes = []
    bridge = FakeBridge([json.dumps({"tool": "start_mission", "args": {"title": "Clinic leads", "request": "Find clinics"}}),
                         json.dumps({"done": "On it, I've started a mission for that."})])
    local = Script()  # must not be used
    ask = clair_ask(bridge, local, lambda mode, why: modes.append(mode))
    clair = ClairAgent(toolbox=build_toolbox(**deps, agent_name="clair"), ask=ask, approvals=None,
                       events=EventLog(tmp_path / "e.db"), context_fn=lambda: "status", memory=None)
    reply = clair.chat([{"sender": "patrick", "text": "Find me some clinic leads"}])
    assert reply == "On it, I've started a mission for that."
    assert opened[0]["source"] == "clair" and bridge.calls == 2 and local.seen == [] and modes == ["cloudflare"] * 2
    assert "tell_patrick" not in build_toolbox(**deps, agent_name="clair").tools


def test_clair_falls_back_to_local_brain(tmp_path):
    deps, *_ = stores(tmp_path)
    modes = []
    local = Script({"done": "Local me here."})
    ask = clair_ask(FakeBridge(error=BridgeError("Gemini's free-tier limit was hit")), local, lambda m, w: modes.append((m, w)))
    clair = ClairAgent(toolbox=build_toolbox(**deps, agent_name="clair"), ask=ask, approvals=None,
                       events=EventLog(tmp_path / "e.db"), context_fn=lambda: "s", memory=None)
    assert clair.chat([{"sender": "patrick", "text": "hi"}]) == "Local me here."
    assert modes == [("fallback", "Gemini's free-tier limit was hit")]


def test_tell_architect_hands_off(tmp_path):
    deps, *_ = stores(tmp_path)
    soon = []
    commander = SimpleNamespace(soon=lambda reason, delay_seconds=120: soon.append(reason))
    box = build_toolbox(**deps, agent_name="clair", commander=commander)
    assert "Passed to The Architect" in box.tools["tell_architect"].fn("Patrick wants a plan for the clinic leads")
    from datetime import datetime, timedelta, timezone
    due = deps["followups"].due(datetime.now(timezone.utc) + timedelta(hours=1))
    assert due[0]["note"] == "From clair: Patrick wants a plan for the clinic leads" and soon


def test_bridge_step(monkeypatch):
    from wetworks.bridge import ClairBridge

    sent = {}

    def post(url, headers, json, timeout):
        sent.update(json)
        return SimpleNamespace(status_code=200, text="", json=lambda: {"text": '{"done":"hi"}'})

    monkeypatch.setattr("wetworks.bridge.httpx.post", post)
    assert ClairBridge("https://x/wetworks", "t").step("SYS", "P", 500) == '{"done":"hi"}'
    assert sent == {"mode": "step", "system": "SYS", "prompt": "P", "max_tokens": 500}
    monkeypatch.setattr("wetworks.bridge.httpx.post", lambda *a, **k: SimpleNamespace(status_code=403, text='{"error":"Origin not allowed"}'))
    import pytest
    with pytest.raises(BridgeError, match="deploy the updated"):
        ClairBridge("https://x/wetworks", "t").step("S", "P")


# ---------------- Intel ----------------

TOPICS = """
[intel]
[[topics]]
name = "AI"
brief = "models"
max_items = 1
queries = ["new models"]
"""


def test_intel_agent_records_only_real_urls(tmp_path):
    (tmp_path / "intel.toml").write_text(TOPICS)
    ask = Script({"tool": "search_web", "args": {"query": "open model release this week"}},
                 {"tool": "record_finding", "args": {"title": "Fake", "summary": "x", "url": "https://made.up/news"}},
                 {"tool": "read_page", "args": {"url": "https://news.example/x"}},
                 {"tool": "record_finding", "args": {"title": "Model X", "summary": "New open model.", "url": "https://news.example/x", "importance": 4}},
                 {"tool": "record_finding", "args": {"title": "More", "summary": "y", "url": "https://sunrisespa.example/jobs"}},
                 {"done": "One finding."})
    c = ctx(tmp_path, ask)
    found = Intel(c, topics_path=tmp_path / "intel.toml").run()
    assert [(f.title, f.url, f.importance) for f in found] == [("Model X", "https://news.example/x", 4)]
    prompts = [p for _, p in ask.seen]
    assert "didn't come up in your searches" in prompts[2] and "the limit" in prompts[5]
    assert c.llm.queries == ["open model release this week"]  # it wrote its own query


def test_intel_falls_back_when_model_cant_use_tools(tmp_path):
    (tmp_path / "intel.toml").write_text(TOPICS)
    ask = Script({"done": "Nothing to do."},  # agent never searches
                 json.dumps({"findings": [{"index": 0, "title": "Model X", "summary": "s", "importance": 3}]}))

    c = ctx(tmp_path, ask)
    c.llm.ask_local = lambda agent, prompt, **kw: SimpleNamespace(text=ask(None, prompt, 0))
    found = Intel(c, topics_path=tmp_path / "intel.toml").run()
    assert [f.title for f in found] == ["Model X"]  # the fixed routine took over


# ---------------- Revenue ----------------

def test_revenue_agent_records_leads_and_drafts(tmp_path):
    ask = Script({"tool": "search_web", "args": {"query": "Sanford spa hiring front desk"}},
                 {"tool": "record_lead", "args": {"business": "Sunrise Spa", "url": "https://sunrisespa.example/jobs",
                                                  "signal": "Hiring a receptionist for bookings", "fit": 5, "offer": "automation"}},
                 {"tool": "record_lead", "args": {"business": "Sunrise Spa", "url": "https://sunrisespa.example/jobs", "signal": "dup", "fit": 5}},
                 {"tool": "record_lead", "args": {"business": "Ghost LLC", "url": "https://ghost.example", "signal": "x", "fit": 5}},
                 {"done": "One lead."})
    c = ctx(tmp_path, ask)
    leads = LeadStore(tmp_path / "leads.db")
    rev = Revenue(c, leads, config_path=ROOT / "config" / "revenue.toml")
    rev.draft = lambda lead: leads.update(lead["id"], draft="Subject: hi\n\nbody") and "x"
    found = rev.run()
    rows = leads.recent()
    assert [(r["business"], r["offer"], r["fit"]) for r in rows] == [("Sunrise Spa", "Automation", 5)]
    assert rows[0]["draft"].startswith("Subject") and found[0].title == "Sunrise Spa: Automation"
    prompts = [p for _, p in ask.seen]
    assert "Already in the pipeline" in prompts[3] and "didn't come up in your searches" in prompts[4]


# ---------------- Splinter ----------------

def test_splinter_research_agent_then_writes(tmp_path):
    ask = Script({"tool": "search_web", "args": {"query": "Orlando clinics AI intake vendors"}},
                 {"tool": "read_page", "args": {"url": "https://sunrisespa.example/jobs"}},
                 {"done": "A short list of clinics with an approach for each."})
    c = ctx(tmp_path, ask)
    writer_prompts = []
    c.llm.ask_local = lambda agent, prompt, **kw: writer_prompts.append(prompt) or SimpleNamespace(
        text="## Leads\n- Sunrise Spa needs booking help [1]\nNext step: email them.")
    store = MissionStore(tmp_path / "missions.db")
    op = SplinterOperator(store, Splinter(c, store))
    m = store.create("Clinic leads", "Find Orlando clinics wanting AI intake", "clair")
    done = op.run_one(store.get(m["id"]))
    assert done["status"] == "done", done["result"]
    assert done["sources"][0]["url"] == "https://sunrisespa.example/jobs"  # the page it read comes first
    assert "Full text of https://sunrisespa.example/jobs" in writer_prompts[0]
    assert "A short list of clinics" in writer_prompts[0]
    steps = [s["text"] for s in done["steps"]]
    assert "Searching: Orlando clinics AI intake vendors" in steps and "Reading https://sunrisespa.example/jobs" in steps
