import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from wetworks.cells.base import CellContext
from wetworks.cells.observer import ObserverCell
from wetworks.config import Settings
from wetworks.events import EventLog
from wetworks.findings import FindingsStore
from wetworks.llm import Reply
from wetworks.observer import Observer

CONFIG = Path(__file__).parent.parent / "config" / "observer.toml"
TZ = ZoneInfo("America/New_York")


def at(day, hh, mm, ss=0):
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=TZ).astimezone(timezone.utc).isoformat()


def workday(obs, day, start=(8, 0), apps=(("Mail", 10), ("Code", 60), ("Safari", 20), ("Code", 40)), device="mac"):
    """Feed a day of 5-second samples: each (app, minutes) block in order."""
    h, m = start
    t = datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)
    samples = []
    for app, minutes in apps:
        for _ in range(minutes * 12):
            samples.append({"ts": t.isoformat(), "app": app, "title": f"{app} window", "secs": 5})
            t += timedelta(seconds=5)
    obs.add_samples(device, "mac", samples)


def make(tmp_path):
    return Observer(tmp_path / "obs.db", CONFIG, tz="America/New_York")


def test_sensitive_screens_are_watched_but_details_never_kept(tmp_path):
    o = make(tmp_path)
    today = datetime.now(TZ).date()
    o.add_samples("msi", "windows", [
        {"ts": at(today, 0, 1), "app": "chrome", "title": "Chase - Accounts ending 4421", "secs": 5},
        {"ts": at(today, 0, 2), "app": "1Password", "title": "Personal vault", "secs": 5},
        {"ts": at(today, 0, 3), "app": "mstsc", "title": "NovaPACS - SMITH^JOHN worklist", "secs": 5},
        {"ts": at(today, 0, 4), "app": "Code", "title": "floor.js — wetworks", "secs": 5},
    ])
    rows = o.db.execute("SELECT app, title FROM samples ORDER BY id").fetchall()
    assert rows == [("chrome", "(sensitive)"), ("1Password", "(sensitive)"), ("mstsc", "(work screen)"), ("Code", "floor.js — wetworks")]
    o.add_shot("msi", "chrome", "Capital One - Statement",
               'Reviewing a Capital One statement showing a balance of $4,812.33 for card 5521 and a "Pay Jane Doe" note')
    o.add_shot("msi", "mstsc", "NovaPACS", "Checking a PACS worklist for patient Jane Roe, MRN 12345678, DOB 01/02/1960")
    saved = " ".join(r[0] + r[1] for r in o.db.execute("SELECT title, description FROM shots").fetchall())
    for gone in ("4,812", "5521", "Jane Doe", "Jane Roe", "12345678", "01/02/1960", "Capital One - Statement", "NovaPACS"):
        assert gone not in saved, gone
    assert "statement" in saved.lower() and "worklist" in saved.lower()


def test_never_watch_lists_block_capture(tmp_path):
    cfg = tmp_path / "observer.toml"
    cfg.write_text(CONFIG.read_text().replace("watch_work_screens = true", "watch_work_screens = false")
                   .replace("never_watch_apps = []", 'never_watch_apps = ["Signal"]'))
    o = Observer(tmp_path / "obs.db", cfg, tz="America/New_York")
    today = datetime.now(TZ).date()
    o.add_samples("mac", "mac", [{"ts": at(today, 0, 1), "app": "Signal", "title": "Chat", "secs": 5},
                                 {"ts": at(today, 0, 2), "app": "mstsc", "title": "NovaPACS", "secs": 5}])
    assert o.db.execute("SELECT app, title FROM samples").fetchall() == [("Private", ""), ("Private", "")]
    assert not o.add_shot("mac", "mstsc", "NovaPACS worklist", "Checking a worklist")
    h = o.helper_config("mac")
    assert "Signal" in h["never_apps"] and "novarad" in h["never_title_words"]


def test_day_summary(tmp_path):
    o = make(tmp_path)
    today = datetime.now(TZ).date()
    workday(o, today, start=(0, 5))
    d = o.day(today)
    assert d["active_minutes"] == 130 and d["first"] == "00:05"
    assert [b["app"] for b in d["focus_blocks"]] == ["Code", "Code"] and d["switches"] == 3
    assert d["apps"][0] == {"app": "Code", "minutes": 100}


def test_patterns_and_suggestions_need_approval(tmp_path):
    o = make(tmp_path)
    today = datetime.now(TZ).date()
    for i in range(1, 8):
        workday(o, today - timedelta(days=i), start=(9, 30))
    p = o.patterns(today=today)
    assert p["days_seen"] == 7 and p["usual_start"] == "09:30" and p["morning_routine"][:2] == ["Mail", "Code"]
    requested = []
    approvals = SimpleNamespace(request=lambda agent, tool, args, summary, reason="": requested.append((tool, args, summary)))
    s = Settings(dry_run=True, data_dir=tmp_path, morning_brief="07:00", evening_brief="21:00")
    ctx = CellContext(settings=s, llm=SimpleNamespace(paused=threading.Event()), findings=FindingsStore(tmp_path / "f.db"))
    cell = ObserverCell(ctx, o, approvals)
    assert cell.suggest() >= 2
    kinds = {a["kind"] for _, a, _ in requested}
    assert "brief_time" in kinds and "mission" in kinds
    assert any(a.get("time") == "09:15" for _, a, _ in requested)  # 15 min before he usually starts
    assert cell.suggest() == 0  # never asked twice


class FakeLLM:
    def __init__(self):
        self.paused = threading.Event()
        self.images = []
        self.ollama = SimpleNamespace(describe_image=lambda img, prompt: self.images.append(img) or
                                      "Editing floor.js in VS Code for the Wetworks console; call Bob at 407-555-0199.")

    def ask_local(self, agent, prompt, **kw):
        return Reply(text="1. Open the console\n2. Export the CSV", model="local:fake", cost_usd=0.0)

    def ask(self, *a, **k):
        raise AssertionError("The Observer must never use a cloud model")


def test_screenshots_described_locally_then_dropped_and_playbooks(tmp_path):
    o = make(tmp_path)
    llm = FakeLLM()
    cell = ObserverCell(CellContext(settings=Settings(dry_run=False, data_dir=tmp_path), llm=llm,
                                    findings=FindingsStore(tmp_path / "f.db"), events=EventLog(tmp_path / "e.db")), o)
    text = cell.describe(b"\xff\xd8jpeg", "mac", "Code", "floor.js — wetworks")
    stored = o.db.execute("SELECT description FROM shots").fetchone()[0]
    assert "VS Code" in stored and "407-555-0199" not in stored  # personal details scrubbed
    assert not list(tmp_path.rglob("*.jpg"))                     # image never written
    llm.paused.set()
    assert cell.describe(b"x", "mac", "Code", "x") is None       # Forge training: skip
    llm.paused.clear()
    r = o.start_recording("mac", "Export bank CSV")
    assert o.helper_config("mac")["recording"]["id"] == r["id"]
    cell.describe(b"x", "mac", "Safari", "Statements", recording=r["id"])
    o.stop_recording(r["id"])
    book = cell.make_playbook(r["id"])
    rec = o.recording(r["id"])
    assert rec["status"] == "ready" and rec["playbook"] == book and len(rec["steps"]) == 1


def test_pause_and_api(tmp_path):
    from wetworks.api import create_app
    from wetworks.main import build

    from .test_console import call

    s = Settings(dry_run=True, data_dir=tmp_path, console_token="secret", config_dir="config")
    architect, cells, comms = build(s)
    app = create_app(settings=s, architect=architect, cells=cells, comms=comms, events=architect.events,
                     observer=comms.observer, open_mission=comms.open_mission_fn)
    assert call(app, "GET", "/api/observer/helper?device=mac")["status"] == 401
    cfg = call(app, "GET", "/api/observer/helper?device=mac", token="secret")["json"]
    assert cfg["enabled"] and cfg["never_apps"] == [] and "novarad" not in cfg["never_title_words"]
    now = datetime.now(timezone.utc).isoformat()
    r = call(app, "POST", "/api/observer/samples", token="secret",
             body={"device": "mac", "platform": "mac", "samples": [{"ts": now, "app": "Code", "title": "x", "secs": 5}]})
    assert r["json"]["stored"] == 1
    ov = call(app, "GET", "/api/observer", token="secret")["json"]
    assert ov["devices"][0]["name"] == "mac" and ov["devices"][0]["live"]
    call(app, "POST", "/api/observer/pause", token="secret", body={"paused": True})
    assert call(app, "GET", "/api/observer/helper?device=mac", token="secret")["json"]["enabled"] is False
    rec = call(app, "POST", "/api/observer/recordings", token="secret", body={"device": "mac", "name": "Weekly invoice"})["json"]
    assert rec["status"] == "recording"
    assert call(app, "POST", f"/api/observer/recordings/{rec['id']}/mission", token="secret", body={})["status"] == 409
    assert call(app, "POST", "/api/observer/forget", token="secret", body={"what": "all"})["json"]["deleted"] >= 1


def test_approved_suggestions_apply_and_cloud_sees_nothing(tmp_path):
    from wetworks.main import build, load_overrides

    s = Settings(dry_run=True, data_dir=tmp_path, config_dir="config")
    architect, cells, comms = build(s)
    run = comms.approvals.executors["adopt_observation"]
    assert "restarts" in run(kind="brief_time", which="morning", time="08:45")
    s2 = Settings(dry_run=True, data_dir=tmp_path, config_dir="config")
    load_overrides(s2)
    assert s2.morning_brief == "08:45"
    assert "Opened mission" in run(kind="mission", title="Morning prep", request="Gather what Patrick checks first")
    assert "isn't shared" in comms.clair_agent.__self__.toolbox.tools["observer_today"].fn()
    assert "active_minutes" in comms.commander.toolbox.tools["observer_today"].fn()
    assert comms.drills.tools["clair"].tools["observer_today"].fn()["doing_lately"] == ["Sample only"]
