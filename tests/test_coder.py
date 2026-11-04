import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wetworks.cells.base import CellContext
from wetworks.cells.coder import Coder
from wetworks.code.brain import LocalBrain, clean_edits, make_brain, parse_json_obj
from wetworks.code.workspace import Workspace
from wetworks.config import Settings
from wetworks.events import EventLog
from wetworks.findings import FindingsStore
from wetworks.llm import Reply
from wetworks.missions import MissionStore
from wetworks.operators import SplinterOperator

from .test_code_workspace import make_origin

SANDBOX = Path(__file__).parent.parent / "sandbox" / "server.py"


class ScriptedLLM:
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def ask_local(self, agent, prompt, **kw):
        self.prompts.append(prompt)
        return Reply(text=self.answers.pop(0), model="local:fake", cost_usd=0.0)


def sandbox(monkeypatch, runs_dir):
    """Route the Coder's sandbox calls to the real sandbox server code, in-process."""
    monkeypatch.setenv("RUNS_DIR", str(runs_dir))
    spec = importlib.util.spec_from_file_location("sandbox_server", SANDBOX)
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    calls = []

    def post(url, json, timeout):
        calls.append(json["run"])
        assert not (runs_dir / json["run"] / ".git").exists()  # the sandbox never sees git
        res = srv.run_tests(json["run"])
        return SimpleNamespace(status_code=200, json=lambda: res)

    monkeypatch.setattr("wetworks.cells.coder.httpx.post", post)
    return calls


def setup(tmp_path, answers, monkeypatch):
    origin = make_origin(tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    calls = sandbox(monkeypatch, runs)
    llm = ScriptedLLM(answers)
    ws = Workspace(tmp_path / "ws", "chandlerpc/demo", token="t", remote_url=str(origin))
    prs = []
    monkeypatch.setattr(ws, "open_pr", lambda branch, title, body, draft=False: prs.append((branch, title, draft, body)) or "https://github.com/chandlerpc/demo/pull/9")
    ev = EventLog(tmp_path / "e.db")
    ctx = CellContext(settings=Settings(dry_run=True, data_dir=tmp_path), llm=llm,
                      findings=FindingsStore(tmp_path / "f.db"), events=ev)
    store = MissionStore(tmp_path / "m.db")
    coder = Coder(ctx, store, ws, LocalBrain(llm), runs_dir=runs, sandbox_url="http://sandbox:8800")
    op = SplinterOperator(store, splinter=None, events=ev, runners={"code": coder})
    return store, op, ws, origin, prs, calls, llm, runs


PLAN = json.dumps({"files": ["app.py", "not/in/repo.py"], "approach": "Add multiply with a test."})
GOOD = json.dumps({"summary": "Added multiply().", "edits": [
    {"path": "app.py", "content": "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"},
    {"path": "test_app.py", "content": "from app import multiply\n\n\ndef test_multiply():\n    assert multiply(3, 4) == 12\n"}]})
BAD = json.dumps({"summary": "Added multiply().", "edits": [
    {"path": "app.py", "content": "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a + b\n"},
    {"path": "test_app.py", "content": "from app import multiply\n\n\ndef test_multiply():\n    assert multiply(3, 4) == 12\n"}]})


def git_files(origin, branch):
    import subprocess
    return subprocess.run(["git", "ls-tree", "-r", "--name-only", branch], cwd=origin, capture_output=True, text=True).stdout.split()


def test_code_mission_passes_and_opens_pr(tmp_path, monkeypatch):
    store, op, ws, origin, prs, calls, llm, runs = setup(tmp_path, [PLAN, GOOD], monkeypatch)
    m = store.create("Add multiply", "Add a multiply function with a test", "architect", kind="code")
    done = op.run_one(store.get(m["id"]))
    assert done["status"] == "done", done["result"]
    assert done["sources"][0]["url"].endswith("/pull/9") and "Tests pass" in done["result"]
    branch, title, draft, body = prs[0]
    assert branch == f"wetworks/mission-{m['id']}" and title == "[Wetworks] Add multiply" and draft is False
    assert "test_app.py" in git_files(origin, branch) and "test_app.py" not in git_files(origin, "main")
    assert "not/in/repo.py" not in llm.prompts[1]  # only real files are read
    assert len(calls) == 1 and list(runs.iterdir()) == []  # sandbox copy cleaned up


def test_failing_tests_get_one_repair_then_draft(tmp_path, monkeypatch):
    store, op, ws, origin, prs, calls, llm, runs = setup(tmp_path, [PLAN, BAD, BAD], monkeypatch)
    m = store.create("Add multiply", "Add multiply", "console", kind="code")
    done = op.run_one(store.get(m["id"]))
    assert done["status"] == "done" and len(calls) == 2
    assert "Your previous attempt failed its tests" in llm.prompts[2] and "assert" in llm.prompts[2]
    assert prs[0][2] is True and "draft" in done["result"]


def test_repair_that_works(tmp_path, monkeypatch):
    store, op, ws, origin, prs, calls, llm, runs = setup(tmp_path, [PLAN, BAD, GOOD], monkeypatch)
    m = store.create("Add multiply", "Add multiply", "console", kind="code")
    op.run_one(store.get(m["id"]))
    assert len(calls) == 2 and prs[0][2] is False


def test_blocked_edit_fails_mission(tmp_path, monkeypatch):
    sneaky = json.dumps({"summary": "x", "edits": [{"path": ".github/workflows/x.yml", "content": "on: push"}]})
    store, op, ws, origin, prs, calls, llm, runs = setup(tmp_path, [PLAN, sneaky], monkeypatch)
    m = store.create("x", "x", "console", kind="code")
    done = op.run_one(store.get(m["id"]))
    assert done["status"] == "failed" and "can't edit" in done["result"] and not prs
    assert git_files(origin, "main") == ["README.md", "app.py"]


def test_files_written_by_tests_are_not_committed(tmp_path, monkeypatch):
    """Even though the sandbox only gets a copy, make sure nothing unstaged reaches the PR."""
    store, op, ws, origin, prs, calls, llm, runs = setup(tmp_path, [PLAN, GOOD], monkeypatch)
    real_export = ws.export

    def export_and_litter(dest):
        real_export(dest)
        (ws.dir / "litter.txt").write_text("should never be committed")

    monkeypatch.setattr(ws, "export", export_and_litter)
    m = store.create("Add multiply", "Add multiply", "console", kind="code")
    op.run_one(store.get(m["id"]))
    assert "litter.txt" not in git_files(origin, prs[0][0])


def test_no_edits_fails_clearly(tmp_path, monkeypatch):
    store, op, *_ = setup(tmp_path, [PLAN, json.dumps({"summary": "hmm", "edits": []})], monkeypatch)
    m = store.create("x", "x", "console", kind="code")
    done = op.run_one(store.get(m["id"]))
    assert done["status"] == "failed" and "didn't propose any file changes" in done["result"]


def test_brain_helpers():
    assert parse_json_obj('noise ```json\n{"a": 1}\n``` more') == {"a": 1}
    assert parse_json_obj("nope") == {}
    edits = clean_edits([{"path": "a.py", "content": "x"}, {"path": "b.py", "delete": True}, {"path": ""},
                         {"path": "c.py"}, "junk", {"path": "d.py", "content": "y"}], max_files=3)
    assert [e["path"] for e in edits] == ["a.py", "b.py", "d.py"]
    with pytest.raises(ValueError):
        make_brain("gpt", None)


def test_unknown_kind_fails(tmp_path, monkeypatch):
    store, op, *_ = setup(tmp_path, [], monkeypatch)
    m = store.create("x", "x", "console", kind="code")
    op.runners.pop("code")
    assert op.run_one(store.get(m["id"]))["status"] == "failed"
