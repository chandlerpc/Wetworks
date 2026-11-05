import json
from types import SimpleNamespace

import pytest

from wetworks.agent.commander import Commander
from wetworks.agent.runtime import Agent, Tool, Toolbox, parse_decision
from wetworks.agent.state import Approvals, Followups
from wetworks.agent.toolkit import build_toolbox
from wetworks.events import EventLog
from wetworks.findings import FindingsStore
from wetworks.leads import LeadStore
from wetworks.memory import Memory
from wetworks.missions import MissionStore

from .test_console import call, make


class Script:
    """A fake model that answers each agent step from a list, and records what it was shown."""

    def __init__(self, *steps):
        self.steps, self.seen = [json.dumps(s) if isinstance(s, dict) else s for s in steps], []

    def __call__(self, system, prompt, max_tokens):
        self.seen.append((system, prompt))
        return self.steps.pop(0) if self.steps else json.dumps({"done": "out of script"})


def test_agent_uses_tools_then_finishes():
    calls = []
    box = Toolbox([Tool("add", "add two numbers", {"a": "n", "b": "n"}, lambda a, b: calls.append((a, b)) or a + b)])
    ask = Script({"thought": "need sum", "tool": "add", "args": {"a": 2, "b": 3, "junk": 1}},
                 {"thought": "have it", "done": "The answer is 5."})
    run = Agent("t", "You add.", box, ask).run("What's 2+3?")
    assert run.finished and run.final == "The answer is 5." and calls == [(2, 3)]
    assert "Result: 5" in ask.seen[1][1] and "add two numbers" in ask.seen[0][0]


def test_agent_recovers_from_bad_steps_and_tool_errors():
    def boom():
        raise RuntimeError("network down")

    box = Toolbox([Tool("boom", "fails", {}, boom)])
    ask = Script("not json", {"tool": "nope"}, {"tool": "boom", "args": {}}, {"done": "Gave up politely."})
    run = Agent("t", "p", box, ask).run("try")
    obs = [s.observation for s in run.steps]
    assert "wasn't a valid step" in obs[0] and "no tool called 'nope'" in obs[1] and "network down" in obs[2]
    assert run.final == "Gave up politely."


def test_agent_step_limit():
    box = Toolbox([Tool("ping", "p", {}, lambda: "pong")])
    ask = Script(*[{"tool": "ping"}] * 3, {"done": "Summary after limit."})
    run = Agent("t", "p", box, ask, max_steps=3).run("loop forever")
    assert not run.finished and len(run.steps) == 3 and run.final == "Summary after limit."


def test_gated_tool_needs_approval_then_runs(tmp_path):
    ran = []
    asked = []
    approvals = Approvals(tmp_path / "a.db", on_request=asked.append)
    box = Toolbox([Tool("merge_pull_request", "merge", {"pr": "n"}, lambda pr: ran.append(pr) or f"Merged #{pr}",
                        gate="approve", summarize=lambda a: f"Merge PR #{a['pr']}")])
    approvals.executors["merge_pull_request"] = box.tools["merge_pull_request"].fn
    ask = Script({"thought": "tests pass", "tool": "merge_pull_request", "args": {"pr": 12}},
                 {"thought": "again", "tool": "merge_pull_request", "args": {"pr": 12}}, {"done": "Asked Patrick."})
    run = Agent("architect", "p", box, ask, approvals=approvals).run("merge it")
    assert ran == [] and "Approval request #1 sent" in run.steps[0].observation
    assert len(approvals.pending()) == 1 and asked[0]["summary"] == "Merge PR #12"  # duplicate not stacked
    approvals.decide(1, True)
    with pytest.raises(ValueError):
        approvals.decide(1, True)  # can't approve twice
    assert ran == [12] and approvals.get(1)["status"] == "done" and approvals.get(1)["result"] == "Merged #12"


def test_declined_and_failed_approvals(tmp_path):
    a = Approvals(tmp_path / "a.db")
    def fails(**kw):
        raise RuntimeError("github said no")

    a.executors["x"] = fails
    r1 = a.request("architect", "x", {}, "do x")
    assert a.decide(r1["id"], False)["status"] == "declined"
    r2 = a.request("architect", "x", {"v": 1}, "do x again")
    assert a.decide(r2["id"], True)["status"] == "failed" and "github said no" in a.get(r2["id"])["result"]
    r3 = a.request("architect", "unknown_tool", {}, "?")
    assert a.decide(r3["id"], True)["status"] == "failed"


def test_followups(tmp_path):
    from datetime import datetime, timedelta, timezone

    f = Followups(tmp_path / "f.db")
    x = f.add("architect", "check mission 4", 0.25)
    assert f.due() == []
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert [d["note"] for d in f.due(later)] == ["check mission 4"]
    f.close([x["id"]])
    assert f.due(later) == []


def stores(tmp_path):
    missions = MissionStore(tmp_path / "m.db")
    opened = []

    def open_mission(title, request, source, kind="research"):
        m = missions.create(title, request, source=source, kind=kind)
        opened.append(m)
        return m

    sent = []
    deps = dict(llm=SimpleNamespace(web_search_local=lambda q, **k: [{"title": "r", "url": "https://x", "content": q}],
                                    read_page_local=lambda u, **k: ""),
                memory=Memory(tmp_path / "mem.db"), missions=missions, open_mission=open_mission,
                findings=FindingsStore(tmp_path / "f.db"), leads=LeadStore(tmp_path / "l.db"),
                revenue=SimpleNamespace(draft=lambda lead: "Subject: hi\n\nbody"),
                notify=SimpleNamespace(send=sent.append), followups=Followups(tmp_path / "fu.db"))
    return deps, opened, sent


def test_toolkit_limits_and_basics(tmp_path):
    deps, opened, sent = stores(tmp_path)
    box = build_toolbox(**deps, daily_mission_cap=2)
    t = box.tools
    assert "merge_pull_request" not in t  # only offered when a code workspace exists
    assert t["start_mission"].fn("A", "do a") .startswith("Opened mission #")
    assert t["start_mission"].fn("B", "do b", kind="code").startswith("Opened code mission #")
    assert "Daily limit" in t["start_mission"].fn("C", "do c")
    for m in opened:
        deps["missions"].set_status(m["id"], "running")
    deps["missions"].create("D", "d", source="console")
    box2 = build_toolbox(**deps, daily_mission_cap=10)
    assert "Three missions are already" in box2.tools["start_mission"].fn("E", "e")
    lead = deps["leads"].add("Acme", "https://acme.example", "hiring", 5)
    assert t["note_lead"].fn(lead["id"], notes="Owner is Dana") == f"Updated lead #{lead['id']}."
    assert "[architect] Owner is Dana" in deps["leads"].get(lead["id"])["notes"]
    assert t["draft_outreach"].fn(lead["id"]).startswith("Subject")
    assert t["remember"].fn("Patrick likes short briefs") == "Saved."
    assert t["recall"].fn("short briefs") == ["Patrick likes short briefs"]
    t["tell_patrick"].fn("Lead looks hot")
    assert "Lead looks hot" in sent[0]
    assert "Follow-up #" in t["follow_up_later"].fn("check Acme", 2)


def test_commander_cycle_opens_mission_and_reports(tmp_path):
    deps, opened, sent = stores(tmp_path)
    box = build_toolbox(**deps)
    ev = EventLog(tmp_path / "e.db")
    ask = Script({"thought": "check leads", "tool": "list_leads", "args": {}},
                 {"thought": "worth researching", "tool": "start_mission",
                  "args": {"title": "Research Acme", "request": "Find who runs Acme and what they use"}},
                 {"done": "Opened #1 to research Acme before outreach."})
    cmd = Commander(toolbox=box, ask=ask, approvals=Approvals(tmp_path / "a.db"), followups=deps["followups"],
                    events=ev, context_fn=lambda: "Spend $0.43", memory=deps["memory"])
    deps["followups"].add("architect", "look at Acme", 0.25)
    report = cmd.cycle("manual")
    assert report == "Opened #1 to research Acme before outreach."
    assert opened[0]["source"] == "architect" and opened[0]["title"] == "Research Acme"
    assert "Spend $0.43" in ask.seen[0][1] and "architect" not in ev.busy
    assert cmd.cycle("scheduled check") == "Skipped: a cycle ran recently."


def test_architect_chat_is_agentic(tmp_path):
    app, architect, comms = make(tmp_path)
    deps, opened, _ = stores(tmp_path)
    ask = Script({"tool": "list_missions", "args": {}}, {"done": "No missions yet. Want me to start one?"})
    cmd = Commander(toolbox=build_toolbox(**deps), ask=ask, approvals=Approvals(tmp_path / "a.db"),
                    followups=deps["followups"], events=architect.events, context_fn=lambda: "s", memory=None)
    comms.architect_agent = cmd.chat
    comms.post("architect", "What's running?", wait=True)
    assert comms.messages("architect")[-1]["text"] == "No missions yet. Want me to start one?"
    assert "Patrick: What's running?" in ask.seen[0][1]


def test_approvals_api(tmp_path):
    app, architect, comms = make(tmp_path)
    from wetworks.api import create_app

    a = Approvals(tmp_path / "a.db")
    a.executors["merge_pull_request"] = lambda pr: f"Merged #{pr}"
    req = a.request("architect", "merge_pull_request", {"pr": 3}, "Merge PR #3")
    app = create_app(settings=architect.s, architect=architect, cells={}, comms=comms, events=architect.events, approvals=a)
    assert call(app, "GET", "/api/approvals", token="secret")["json"]["pending"][0]["summary"] == "Merge PR #3"
    assert call(app, "POST", f"/api/approvals/{req['id']}", token="secret", body={"decision": "maybe"})["status"] == 400
    r = call(app, "POST", f"/api/approvals/{req['id']}", token="secret", body={"decision": "approve"})
    assert r["json"]["status"] == "done" and r["json"]["result"] == "Merged #3"
    assert call(app, "POST", f"/api/approvals/{req['id']}", token="secret", body={"decision": "approve"})["status"] == 409
    assert call(app, "GET", "/api/approvals")["status"] == 401


def test_parse_decision():
    assert parse_decision('```json\n{"done": "x"}\n```') == {"done": "x"}
    assert parse_decision("I think {\"tool\": \"a\"} works") == {"tool": "a"}
    assert parse_decision("nope") == {}


def test_architect_can_think_on_claude_while_cells_stay_local(tmp_path):
    from wetworks.config import Settings
    from wetworks.main import build

    s = Settings(dry_run=True, data_dir=tmp_path, config_dir="config", architect_brain="claude-api", anthropic_api_key="test-key")
    architect, cells, comms = build(s)
    c = comms.commander
    assert (c.max_steps, c.chat_steps, c.step_tokens, c.uses_gpu) == (14, 10, 3500, False)
    assert c.ask("sys", "hello", 50).startswith("[dry-run claude-opus-5-5]")         # Opus for The Architect
    assert "[dry-run local:" in cells["intel"].ctx.ask("sys", "hello", 50)               # cells stay free
    assert "aren't shared" in c.toolbox.tools["finance_summary"].fn()                 # private data stays local
    assert "isn't shared" in c.toolbox.tools["dayjob_search"].fn(query="x")
    architect.llm.paused.set()                                                         # Forge training
    assert "Skipped" not in c.cycle("manual")


def test_checkins_twice_a_day_on_sonnet_and_only_major_events_jump_the_queue(tmp_path):
    from wetworks.config import Settings
    from wetworks.findings import Finding
    from wetworks.main import build

    s = Settings(dry_run=True, data_dir=tmp_path, config_dir="config", architect_brain="claude-api", anthropic_api_key="test-key")
    architect, cells, comms = build(s)
    c = comms.commander
    assert c.cycle_ask("sys", "hi", 20).startswith("[dry-run claude-sonnet-5-5]")   # check-ins on Sonnet
    assert c.ask("sys", "hi", 20).startswith("[dry-run claude-opus-5-5]")          # chatting with Patrick on Opus
    queued = []
    c.soon = lambda reason, delay_seconds=120: queued.append(reason)
    c.on_mission_done({"id": 1, "status": "done", "kind": "research", "title": "Clinic list"})
    assert queued == []                                                             # routine: waits for the check-in
    c.on_mission_done({"id": 2, "status": "failed", "kind": "research", "title": "Broken"})
    c.on_mission_done({"id": 3, "status": "done", "kind": "code", "title": "Footer"})
    architect.findings.add(Finding(cell="intel", topic="AI models & tools", title="Minor", summary="", url="https://a.example/1", importance=3))
    architect.findings.add(Finding(cell="intel", topic="Money opportunities", title="Deadline today", summary="", url="https://a.example/2", importance=5))
    assert [q.split(":")[1].strip()[:4] for q in queued] == ["miss", "code", "Mone"]
    architect.findings.add(Finding(cell="revenue", topic="Revenue leads", title="Fit-5 clinic", summary="", url="https://a.example/3", importance=4))
    assert len(queued) == 3                                                          # 3 extra a day, then it waits
