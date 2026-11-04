import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wetworks.cells.base import CellContext
from wetworks.cells.learner import Learner
from wetworks.comms import Comms, parse_remember
from wetworks.config import Settings
from wetworks.events import EventLog
from wetworks.findings import FindingsStore
from wetworks.llm import Reply
from wetworks.memory import Memory, archive_old_missions
from wetworks.missions import MissionStore

from .test_console import call, make

PROFILE = Path(__file__).parent.parent / "config" / "profile.md"


class FakeLLM:
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []
        self.s = Settings(dry_run=True)
        from wetworks.ledger import Ledger
        import tempfile
        self.ledger = Ledger(Path(tempfile.mkdtemp()) / "l.db")

    def ask_local(self, agent, prompt, **kw):
        self.prompts.append(prompt)
        return Reply(text=self.answers.pop(0), model="local:fake", cost_usd=0.0)


def test_add_search_forget(tmp_path):
    m = Memory(tmp_path / "m.db")
    a = m.add("Patrick prefers morning briefs under 120 words.", kind="preference")
    assert a and a["kind"] == "preference"
    assert m.add("patrick prefers  morning briefs under 120 words.") is None  # duplicate
    assert m.add("  ") is None
    m.add("Gregg runs a community climbing gym and is a SummitFlow contact.", kind="person")
    hits = m.search("what do you know about Gregg's gym?")
    assert [h["text"][:5] for h in hits] == ["Gregg"]
    assert m.search("the and of") == []  # stopwords only
    assert m.forget(a["id"]) and not m.forget(a["id"])
    assert m.search("briefs") == []  # removed from the search index too


def test_profile_sync_is_pinned_and_replaceable(tmp_path):
    m = Memory(tmp_path / "m.db")
    n = m.sync_profile(PROFILE)
    assert n >= 15 and all(x["pinned"] for x in m.pinned())
    assert m.sync_profile(PROFILE) == n and m.count() == n  # re-sync replaces, doesn't duplicate
    m.add("A chat fact about CodeVoyage pricing tiers")
    ctx = m.context_for("CodeVoyage pricing")
    assert ctx.startswith("About Patrick:") and "CodeVoyage pricing tiers" in ctx.split("may be relevant:")[1]
    p = tmp_path / "p.md"
    p.write_text("# x\n- Only line\n")
    m.sync_profile(p)
    assert [x["text"] for x in m.pinned()] == ["Only line"]


def test_chat_remember_and_recall(tmp_path):
    reply = json.dumps({"reply": "Got it.", "mission": None, "remember": ["Patrick wants leads only in Seminole County."]})
    llm = FakeLLM([reply, json.dumps({"reply": "Seminole only.", "mission": None})])
    mem = Memory(tmp_path / "m.db")
    comms = Comms(tmp_path / "c.db", llm, EventLog(tmp_path / "e.db"), lambda: "status", open_mission=lambda *a: None, memory=mem)
    comms.post("architect", "From now on only look for leads in Seminole County.", wait=True)
    assert mem.search("Seminole leads")[0]["source"] == "chat:architect"
    comms.post("architect", "Where should Revenue look for leads?", wait=True)
    assert "Patrick wants leads only in Seminole County." in llm.prompts[1]  # recalled into the next prompt
    assert parse_remember('{"reply":"x","remember":"not a list"}') == []


def test_learner_learns_and_archives(tmp_path):
    llm = FakeLLM([json.dumps({"memories": [
        {"title": "Patrick plans to upgrade to Claude Max on Friday.", "kind": "decision"},
        {"title": "Patrick plans to upgrade to Claude Max on Friday.", "kind": "decision"},
        {"title": "Gregg runs a community climbing gym.", "kind": "person"}]})])
    ev = EventLog(tmp_path / "e.db")
    mem = Memory(tmp_path / "m.db")
    ctx = CellContext(settings=Settings(dry_run=True, data_dir=tmp_path), llm=llm,
                      findings=FindingsStore(tmp_path / "f.db"), events=ev, memory=mem)
    comms = Comms(tmp_path / "c.db", llm, ev, lambda: "s")
    comms._insert("clair", "patrick", "I'm upgrading to Max on Friday.")
    comms._insert("clair", "clair", "Grand.")
    store = MissionStore(tmp_path / "missions.db")
    store.create("Clinic leads", "Find clinics", "clair")
    old = store.create("Old one", "x", "console")
    store.set_status(old["id"], "done", result="r")
    nearly = store.create("Nearly old", "y", "console")
    store.set_status(nearly["id"], "done", result="r")
    now = datetime.now(timezone.utc)
    with store.lock:
        store.db.execute("UPDATE missions SET updated = ? WHERE id = ?", ((now - timedelta(days=400)).isoformat(), old["id"]))
        store.db.execute("UPDATE missions SET updated = ? WHERE id = ?", ((now - timedelta(days=361)).isoformat(), nearly["id"]))
        store.db.commit()

    found = Learner(ctx, comms, store).run(now=now)
    assert {x["text"] for x in mem.recent()} == {"Patrick plans to upgrade to Claude Max on Friday.", "Gregg runs a community climbing gym."}
    prompt = llm.prompts[0]
    assert "I'm upgrading to Max on Friday." in prompt and "Grand." not in prompt  # only Patrick's words
    assert "Clinic leads: Find clinics" in prompt
    assert store.get(old["id"]) is None and store.get(nearly["id"]) is not None
    assert store.db.execute("SELECT COUNT(*) FROM missions_archive").fetchone()[0] == 1
    assert found and found[0].title == "1 mission(s) will be archived next week"


def test_learner_quiet_day(tmp_path):
    llm = FakeLLM([])
    ctx = CellContext(settings=Settings(dry_run=True, data_dir=tmp_path), llm=llm,
                      findings=FindingsStore(tmp_path / "f.db"), events=EventLog(tmp_path / "e.db"), memory=Memory(tmp_path / "m.db"))
    comms = Comms(tmp_path / "c.db", llm, EventLog(tmp_path / "e2.db"), lambda: "s")
    assert Learner(ctx, comms, MissionStore(tmp_path / "missions.db")).run() == [] and llm.prompts == []


def test_memory_api(tmp_path):
    app, architect, comms = make(tmp_path)
    from wetworks.api import create_app

    mem = Memory(tmp_path / "mem.db")
    app = create_app(settings=architect.s, architect=architect, cells={}, comms=comms, events=architect.events, memory=mem)
    r = call(app, "POST", "/api/memory", token="secret", body={"text": "Prefers dark UIs", "kind": "preference"})
    assert r["status"] == 201
    assert call(app, "POST", "/api/memory", token="secret", body={"text": "Prefers dark UIs"})["status"] == 400
    assert call(app, "GET", "/api/memory?q=dark", token="secret")["json"]["memories"][0]["text"] == "Prefers dark UIs"
    assert call(app, "DELETE", f"/api/memory/{r['json']['id']}", token="secret")["status"] == 200
    assert call(app, "DELETE", f"/api/memory/{r['json']['id']}", token="secret")["status"] == 404
    assert call(app, "GET", "/api/memory")["status"] == 401
