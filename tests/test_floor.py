from wetworks.api import create_app
from wetworks.config import Settings
from wetworks.halt import Halt
from wetworks.main import build

from .test_console import call


def make(tmp_path):
    s = Settings(dry_run=True, data_dir=tmp_path, console_token="secret", config_dir="config")
    architect, cells, comms = build(s)
    halt = Halt(tmp_path / "HALTED")
    for c in cells.values():
        c.ctx.halt = halt
    app = create_app(settings=s, architect=architect, cells=cells, comms=comms, events=architect.events, halt=halt)
    return app, architect, cells, halt


def test_halt_persists(tmp_path):
    h = Halt(tmp_path / "HALTED")
    assert not h.is_set()
    h.set(True)
    assert Halt(tmp_path / "HALTED").is_set()  # survives a restart
    h.set(False)
    assert not Halt(tmp_path / "HALTED").is_set()


def test_floor_feed_shape(tmp_path):
    app, architect, _, _ = make(tmp_path)
    assert call(app, "GET", "/api/floor")["status"] == 401
    d = call(app, "GET", "/api/floor", token="secret")["json"]
    ids = {a["id"] for a in d["agents"]}
    assert {"architect", "clair", "intel", "revenue", "learner"} <= ids
    rooms = {r["id"]: r for r in d["rooms"]}
    assert rooms["forge"]["sealed"] is False and rooms["finance"]["sealed"] is False and rooms["dayjob"]["sealed"] is False and rooms["gamedev"]["sealed"] is True
    assert all(a["room"] in rooms for a in d["agents"])
    assert d["hud"]["halted"] is False and "spend" in d["hud"] and "budget" in d["hud"]


def test_floor_marks_errors_blocked(tmp_path):
    app, architect, _, _ = make(tmp_path)
    architect.events.add("intel", "error", "search failed")
    d = call(app, "GET", "/api/floor", token="secret")["json"]
    intel = next(a for a in d["agents"] if a["id"] == "intel")
    assert intel["state"] == "blocked"


def test_kill_switch_needs_confirm_and_stops_cells(tmp_path):
    app, architect, cells, halt = make(tmp_path)
    assert call(app, "POST", "/api/halt", token="secret", body={"halted": True})["status"] == 400
    assert call(app, "POST", "/api/halt", token="secret", body={"halted": "yes", "confirm": "HALT"})["status"] == 400
    r = call(app, "POST", "/api/halt", token="secret", body={"halted": True, "confirm": "HALT"})
    assert r["status"] == 200 and r["json"]["halted"] is True and halt.is_set()
    assert call(app, "GET", "/api/floor", token="secret")["json"]["hud"]["halted"] is True
    assert cells["intel"].safe_run() == []  # nothing runs while halted
    r = call(app, "POST", "/api/halt", token="secret", body={"halted": False})
    assert r["status"] == 200 and not halt.is_set()
