import asyncio
import json

from wetworks.api import create_app
from wetworks.config import Settings
from wetworks.main import build


def call(app, method, path, token=None, body=None):
    """Minimal ASGI client so tests don't need httpx."""
    path, _, qs = path.partition("?")
    headers = [(b"content-type", b"application/json")]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    payload = json.dumps(body).encode() if body is not None else b""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": qs.encode(),
             "headers": headers, "client": ("test", 1), "server": ("test", 80), "root_path": ""}
    out = {"status": None, "body": b""}
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await asyncio.sleep(3600)

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    asyncio.run(app(scope, receive, send))
    try:
        out["json"] = json.loads(out["body"] or b"null")
    except ValueError:
        out["json"] = None
    return out


def make(tmp_path, token="secret"):
    s = Settings(dry_run=True, data_dir=tmp_path, console_token=token, config_dir="config")
    architect, cells, comms = build(s)
    app = create_app(settings=s, architect=architect, cells=cells, comms=comms, events=architect.events)
    return app, architect, comms


def test_api_requires_token(tmp_path):
    app, *_ = make(tmp_path)
    assert call(app, "GET", "/api/status")["status"] == 401
    assert call(app, "GET", "/api/status", token="wrong")["status"] == 401
    r = call(app, "GET", "/api/status", token="secret")
    assert r["status"] == 200 and r["json"]["provider"] == "local"


def test_api_locked_when_no_token_configured(tmp_path):
    app, *_ = make(tmp_path, token="")
    assert call(app, "GET", "/api/status", token="")["status"] == 401
    assert call(app, "GET", "/api/status", token="anything")["status"] == 401


def test_console_page_is_public(tmp_path):
    app, *_ = make(tmp_path)
    r = call(app, "GET", "/")
    assert r["status"] == 200 and b"Wetworks" in r["body"]


def test_comms_round_trip(tmp_path):
    app, architect, comms = make(tmp_path)
    r = call(app, "POST", "/api/comms/clair", token="secret", body={"text": "status?"})
    assert r["status"] == 202
    comms.post("architect", "focus?", wait=True)  # synchronous reply for the test
    msgs = call(app, "GET", "/api/comms/architect", token="secret")["json"]["messages"]
    assert [m["sender"] for m in msgs] == ["patrick", "architect"]
    assert msgs[1]["text"].startswith("[dry-run local:")
    assert call(app, "POST", "/api/comms/clair", token="secret", body={"text": "  "})["status"] == 400
    assert call(app, "GET", "/api/comms/nobody", token="secret")["status"] == 404


def test_events_and_actions(tmp_path):
    app, architect, _ = make(tmp_path)
    architect.events.add("intel", "done", "sweep done")
    ev = call(app, "GET", "/api/events", token="secret")["json"]["events"]
    assert ev[-1]["text"] == "sweep done"
    last = ev[-1]["id"]
    assert call(app, "GET", f"/api/events?after={last}", token="secret")["json"]["events"] == []
    architect.events.busy.add("intel")
    assert call(app, "POST", "/api/actions/intel", token="secret")["status"] == 409
    assert call(app, "POST", "/api/actions/nope", token="secret")["status"] == 404


def test_cell_logs_to_activity_feed(tmp_path):
    _, architect, _ = make(tmp_path)
    from wetworks.cells.intel import Intel
    from wetworks.cells.base import CellContext
    ctx = CellContext(settings=architect.s, llm=architect.llm, findings=architect.findings, events=architect.events)
    Intel(ctx).safe_run()
    kinds = [e["kind"] for e in architect.events.recent()]
    assert kinds[-2:] == ["start", "done"] and "intel" not in architect.events.busy
