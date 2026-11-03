import json
from types import SimpleNamespace

import pytest

from wetworks.cells.base import CellContext
from wetworks.cells.splinter import Splinter
from wetworks.comms import Comms, parse_reply
from wetworks.config import Settings
from wetworks.events import EventLog
from wetworks.findings import FindingsStore
from wetworks.llm import Reply
from wetworks.local import is_public_url
from wetworks.missions import MissionStore
from wetworks.operators import SplinterOperator

from .test_console import call, make


class ScriptedLLM:
    """Answers ask_local calls from a script, in order; records searches and page reads."""

    def __init__(self, answers, results=None, pages=None):
        self.answers = list(answers)
        self.results = results or []
        self.pages = pages or {}
        self.searches, self.prompts = [], []
        self.s = Settings(dry_run=True)

    def ask_local(self, agent, prompt, **kw):
        self.prompts.append(prompt)
        return Reply(text=self.answers.pop(0), model="local:fake", cost_usd=0.0)

    def web_search_local(self, q, **kw):
        self.searches.append(q)
        return self.results

    def read_page_local(self, url, **kw):
        return self.pages.get(url, "")


def setup(tmp_path, llm):
    s = Settings(dry_run=True, data_dir=tmp_path)
    ev = EventLog(tmp_path / "e.db")
    ctx = CellContext(settings=s, llm=llm, findings=FindingsStore(tmp_path / "f.db"), events=ev)
    store = MissionStore(tmp_path / "m.db")
    sent = []
    op = SplinterOperator(store, Splinter(ctx, store), events=ev,
                          notify=SimpleNamespace(send=lambda t: sent.append(t)))
    return store, op, ev, sent


def test_research_mission_end_to_end(tmp_path):
    plan = json.dumps({"needs_research": True, "queries": ["orlando clinic ai intake"], "deliverable": "A lead list"})
    llm = ScriptedLLM(
        [plan, "## Leads\n- Clinic A wants intake automation [1]\nNext step: email Clinic A."],
        results=[{"title": "Clinic A", "url": "https://a.example/post", "content": "snippet"},
                 {"title": "Clinic B", "url": "https://b.example/post", "content": "snippet"}],
        pages={"https://a.example/post": "Clinic A is hiring a vendor for AI intake."},
    )
    store, op, ev, sent = setup(tmp_path, llm)
    m = store.create("Find clinic leads", "Find Orlando clinics that want AI intake automation", "clair")
    done = op.run_one(store.get(m["id"]))

    assert done["status"] == "done" and "Clinic A" in done["result"]
    assert done["sources"][0] == {"n": 1, "title": "Clinic A", "url": "https://a.example/post"}
    assert llm.searches == ["orlando clinic ai intake"]
    assert "Clinic A is hiring a vendor" in llm.prompts[1]  # the page text reached the writer
    assert [s["text"] for s in done["steps"]][0] == "Planning the approach"
    assert sent and "Mission #1 done" in sent[0]
    assert "splinter" not in ev.busy


def test_writing_mission_skips_research(tmp_path):
    llm = ScriptedLLM([json.dumps({"needs_research": False, "queries": [], "deliverable": "An email"}),
                       "Hi Gregg,\nNext step: send it."])
    store, op, *_ = setup(tmp_path, llm)
    m = store.create("Draft email", "Draft a short follow-up email", "architect")
    done = op.run_one(store.get(m["id"]))
    assert done["status"] == "done" and done["sources"] == [] and llm.searches == []


def test_bad_plan_json_still_completes(tmp_path):
    llm = ScriptedLLM(["not json at all", "Here's the answer.\nNext step: none."])
    store, op, *_ = setup(tmp_path, llm)
    m = store.create("", "Explain node wars briefly", "console")
    assert op.run_one(store.get(m["id"]))["status"] == "done"


def test_empty_write_up_fails_cleanly(tmp_path):
    llm = ScriptedLLM([json.dumps({"needs_research": False}), ""])
    store, op, ev, sent = setup(tmp_path, llm)
    m = store.create("x", "y", "console")
    done = op.run_one(store.get(m["id"]))
    assert done["status"] == "failed" and "empty" in done["result"] and not sent
    assert ev.recent()[-1]["kind"] == "error"


def test_cancel_queued_and_running(tmp_path):
    llm = ScriptedLLM([json.dumps({"needs_research": True, "queries": ["q"]})], results=[])
    store, op, *_ = setup(tmp_path, llm)
    a = store.create("a", "a", "console")
    assert store.cancel(a["id"]) and store.get(a["id"])["status"] == "cancelled"
    b = store.create("b", "b", "console")
    store.cancel_requested.add(b["id"])  # simulate a cancel arriving while it runs
    store.set_status(b["id"], "queued")
    assert op.run_one(store.get(b["id"]))["status"] == "cancelled"
    assert not store.cancel(b["id"])  # already finished


def test_recover_requeues_running(tmp_path):
    store = MissionStore(tmp_path / "m.db")
    m = store.create("t", "r", "console")
    store.set_status(m["id"], "running")
    assert store.recover() == 1 and store.next_queued()["id"] == m["id"]
    with pytest.raises(ValueError):
        store.create("t", "   ", "console")


def test_parse_reply():
    assert parse_reply('{"reply": "Hi", "mission": null}') == ("Hi", None)
    text, m = parse_reply('ok {"reply": "On it", "mission": {"title": "Leads", "request": "find leads"}}')
    assert text == "On it" and m["request"] == "find leads"
    assert parse_reply('{"reply": "x", "mission": {"title": "no request"}}') == ("x", None)
    assert parse_reply("plain words") == ("plain words", None)


def test_chat_opens_mission_and_reports_back(tmp_path):
    reply = json.dumps({"reply": "Passing it to The Architect.",
                        "mission": {"title": "Clinic leads", "request": "Find Orlando clinics wanting AI intake"}})
    llm = ScriptedLLM([reply])
    ev = EventLog(tmp_path / "e.db")
    opened = []

    def open_mission(title, request, source):
        opened.append((title, request, source))
        return {"id": 7, "title": title}

    comms = Comms(tmp_path / "c.db", llm, ev, lambda: "status", open_mission=open_mission)
    comms.post("clair", "Can you find me some clinic leads?", wait=True)
    msgs = comms.messages("clair")
    assert opened == [("Clinic leads", "Find Orlando clinics wanting AI intake", "clair")]
    assert "Mission #7 opened: Clinic leads" in msgs[-1]["text"]

    comms.mission_report({"id": 7, "title": "Clinic leads", "status": "done", "source": "clair",
                          "result": "Found three clinics.\n\nMore detail."})
    assert comms.messages("clair")[-1]["text"].startswith("Mission #7 is done: Clinic leads.\n\nFound three clinics.")


def test_mission_api(tmp_path):
    app, architect, comms = make(tmp_path)
    app = __import__("wetworks.api", fromlist=["create_app"]).create_app(
        settings=architect.s, architect=architect, cells={}, comms=comms, events=architect.events,
        missions=comms.missions, open_mission=comms.open_mission_fn)
    r = call(app, "POST", "/api/missions", token="secret", body={"request": "Compare two GPUs"})
    assert r["status"] == 202 and r["json"]["status"] == "queued"
    mid = r["json"]["id"]
    assert call(app, "GET", "/api/missions", token="secret")["json"]["missions"][0]["id"] == mid
    assert call(app, "GET", f"/api/missions/{mid}", token="secret")["json"]["request"] == "Compare two GPUs"
    assert call(app, "POST", f"/api/missions/{mid}/cancel", token="secret")["status"] == 202
    assert call(app, "POST", f"/api/missions/{mid}/cancel", token="secret")["status"] == 409
    assert call(app, "GET", "/api/missions/999", token="secret")["status"] == 404
    assert call(app, "POST", "/api/missions", token="secret", body={"request": ""})["status"] == 400
    assert call(app, "GET", "/api/missions")["status"] == 401


@pytest.mark.parametrize("url,ok", [
    ("http://localhost:11434/api/chat", False),
    ("http://127.0.0.1/", False),
    ("http://169.254.169.254/latest/meta-data", False),
    ("http://192.168.1.74:8700/", False),
    ("http://100.113.64.99:8700/", False),
    ("http://10.0.0.5/", False),
    ("file:///etc/passwd", False),
    ("http://[::1]/", False),
    ("https://8.8.8.8/", True),
])
def test_url_guard(url, ok):
    assert is_public_url(url) is ok


class FakeBridge:
    configured = True

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def ask(self, history, context):
        self.calls.append((history, context))
        if self.error:
            raise self.error
        return self.reply


def test_clair_goes_through_bridge_and_can_open_missions(tmp_path):
    from wetworks.ledger import Ledger

    llm = ScriptedLLM([])  # local model must not be used
    llm.ledger = Ledger(tmp_path / "l.db")
    ev = EventLog(tmp_path / "e.db")
    opened = []
    bridge = FakeBridge(reply=("Passing it on.", {"title": "Leads", "request": "find leads"}))
    comms = Comms(tmp_path / "c.db", llm, ev, lambda: "spend $0.43",
                  open_mission=lambda t, r, s: opened.append((t, r, s)) or {"id": 4, "title": t}, bridge=bridge)
    comms.post("clair", "find me leads", wait=True)
    history, context = bridge.calls[0]
    assert history[-1] == {**history[-1], "sender": "patrick", "text": "find me leads"} and context == "spend $0.43"
    assert opened == [("Leads", "find leads", "clair")]
    assert "Mission #4 opened" in comms.messages("clair")[-1]["text"]
    assert comms.clair_mode == "cloudflare" and llm.prompts == []


def test_clair_falls_back_to_local_when_bridge_down(tmp_path):
    from wetworks.bridge import BridgeError
    from wetworks.ledger import Ledger

    llm = ScriptedLLM([json.dumps({"reply": "Local clAIr here.", "mission": None})])
    llm.ledger = Ledger(tmp_path / "l.db")
    ev = EventLog(tmp_path / "e.db")
    comms = Comms(tmp_path / "c.db", llm, ev, lambda: "s", open_mission=lambda *a: None,
                  bridge=FakeBridge(error=BridgeError("couldn't reach")))
    comms.post("clair", "hi", wait=True)
    assert comms.messages("clair")[-1]["text"] == "Local clAIr here."
    assert comms.clair_mode == "fallback"
    assert any(e["kind"] == "warn" and "answering locally" in e["text"] for e in ev.recent())


def test_architect_channel_never_uses_bridge(tmp_path):
    from wetworks.ledger import Ledger

    llm = ScriptedLLM([json.dumps({"reply": "Architect.", "mission": None})])
    llm.ledger = Ledger(tmp_path / "l.db")
    bridge = FakeBridge(reply=("should not be used", None))
    comms = Comms(tmp_path / "c.db", llm, EventLog(tmp_path / "e.db"), lambda: "s",
                  open_mission=lambda *a: None, bridge=bridge)
    comms.post("architect", "status?", wait=True)
    assert bridge.calls == [] and comms.messages("architect")[-1]["text"] == "Architect."


def test_bridge_client_errors(monkeypatch):
    from wetworks.bridge import BridgeError, ClairBridge

    sent = {}

    def post(url, headers, json, timeout):
        sent.update(url=url, headers=headers, body=json)
        return SimpleNamespace(status_code=401, json=lambda: {})

    monkeypatch.setattr("wetworks.bridge.httpx.post", post)
    b = ClairBridge("https://clair.example.workers.dev/wetworks/", "tok", owner="Patrick")
    with pytest.raises(BridgeError, match="rejected"):
        b.ask([{"sender": "patrick", "text": "hi"}, {"sender": "clair", "text": "yo"}], "ctx")
    assert sent["url"] == "https://clair.example.workers.dev/wetworks"
    assert sent["headers"]["Authorization"] == "Bearer tok"
    assert sent["body"]["messages"] == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    assert not ClairBridge("", "").configured
