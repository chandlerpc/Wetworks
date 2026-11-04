import json

from wetworks.bridge import BridgeError
from wetworks.config import Settings
from wetworks.halt import Halt
from wetworks.main import build


class Gemini:
    """Fake Worker step mode: plays back scripted decisions."""
    configured = True

    def __init__(self, *outs, error=None):
        self.outs, self.error, self.calls = list(outs), error, []

    def step(self, system, prompt, max_tokens):
        self.calls.append((system, prompt))
        if self.error:
            raise self.error
        return self.outs.pop(0) if self.outs else json.dumps({"thought": "done", "done": "All good."})


def setup(tmp_path, bridge, **kw):
    s = Settings(dry_run=True, data_dir=tmp_path, config_dir="config", **kw)
    architect, cells, comms = build(s)
    d = comms.drills
    d.bridge = bridge
    return d, comms, architect


def step(tool, **args):
    return json.dumps({"thought": f"use {tool}", "tool": tool, "args": args})


def test_drill_banks_lessons_and_changes_nothing(tmp_path):
    g = Gemini(step("list_missions"), step("start_mission", title="Find Sanford gyms", request="find them"),
               step("remember", text="Only Seminole County leads"), step("nope_tool"),
               json.dumps({"thought": "reply", "done": "Started a mission for you."}))
    d, comms, architect = setup(tmp_path, g)
    before_missions, before_mem = len(comms.missions.recent(50)), comms.memory.count()
    r = d.run_one("clair", "Find me gyms in Sanford")
    assert r["ok"] and r["steps"] == 5
    assert len(comms.missions.recent(50)) == before_missions  # simulated: no real mission
    assert comms.memory.count() == before_mem                 # simulated: nothing saved
    counts = comms.forge.traces.counts()
    assert counts["gemini"] == {"valid": 4, "total": 5}       # the made-up tool isn't a lesson
    assert comms.forge.lessons()["have"] == 4
    assert "start_mission" in g.calls[0][0] and "C.L.a.i.R" in g.calls[0][0]  # same tools and persona as real
    assert d.stats()["steps_today"] == 5


def test_architect_cycle_drill_and_gated_tools_are_fake(tmp_path):
    g = Gemini(step("switch_local_model", model="wetworks-x:1"), json.dumps({"thought": "ok", "done": "Report."}))
    d, comms, _ = setup(tmp_path, g)
    r = d.run_one("architect", "__cycle__")
    assert r["ok"] and "Run a command cycle" in g.calls[0][1]
    assert comms.approvals.pending() == []  # no real approval request for Patrick


def test_drills_need_gemini_and_respect_limits(tmp_path):
    d, comms, _ = setup(tmp_path, None)
    assert d.run_one()["ok"] is False and "bridge" in d.run_one()["note"]

    d, comms, _ = setup(tmp_path / "b", Gemini(error=BridgeError("Gemini's free-tier limit was hit")))
    r = d.run_one("clair", "hey clair")
    assert not r["ok"] and d.blocked_until
    assert comms.forge.traces.counts() == {}  # failed calls aren't lessons
    assert "resume" in d.ready()[1]

    d, comms, _ = setup(tmp_path / "c", Gemini(), drill_daily_steps=1)
    assert d.run_one("clair", "status?")["ok"]
    assert "budget" in d.ready()[1]

    d, comms, _ = setup(tmp_path / "d", Gemini())
    d.halt = Halt(tmp_path / "d" / "HALTED"); d.halt.set(True)
    assert "halted" in d.run_one()["note"]


def test_pick_rotates_through_requests(tmp_path):
    d, comms, _ = setup(tmp_path, Gemini())
    comms.leads.add("Lake Mary Family Dental", "https://lmfd.example", "hiring", 5)
    pool = d.candidates()
    assert ("clair", "What do we know about Lake Mary Family Dental?") in pool and ("architect", "__cycle__") in pool
    seen = set()
    for _ in range(4):
        agent, req = d.pick()
        d._log(agent, req, 1, True, "")
        seen.add(req)
    assert len(seen) == 4  # least-used first: no repeats until the pool is used
