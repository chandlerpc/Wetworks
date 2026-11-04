import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from wetworks.agent.traces import Traces, recorded
from wetworks.config import Settings
from wetworks.events import EventLog
from wetworks.forge import MIN_EXAMPLES, Forge, ForgeError

ROOT = Path(__file__).parent.parent


class Resp(SimpleNamespace):
    def json(self):
        return self.data


def make(tmp_path, n_teacher=0, n_local=0):
    traces = Traces(tmp_path / "traces.db")
    for i in range(n_teacher):
        traces.record("clair", "gemini", "SYS", f"prompt {i}",
                      json.dumps({"tool": "list_missions", "args": {}}) if i % 2 else json.dumps({"done": f"reply {i}"}))
    for i in range(n_local):
        traces.record("cells", "local", "SYS", f"p{i}", "not json")
    traces.record("clair", "gemini", "SYS", "bad", "garbage")  # invalid teacher output is never used
    llm = SimpleNamespace(paused=threading.Event(), ollama=SimpleNamespace(model="gemma4:12b"))
    f = Forge(settings=Settings(dry_run=True, data_dir=tmp_path, forge_window="1-5"), llm=llm, traces=traces,
              events=EventLog(tmp_path / "e.db"), data_dir=tmp_path / "forge", runner_url="http://forge:8900",
              ollama_url="http://ollama:11434")
    return f, llm


def test_traces_and_recorded_wrapper(tmp_path):
    t = Traces(tmp_path / "t.db")
    ask = recorded(lambda s, p, n: '{"done": "hi"}', t, "clair", lambda: "gemini")
    assert ask("S", "P", 10) == '{"done": "hi"}'
    recorded(lambda s, p, n: "nope", t, "cells", lambda: "local")("S", "P", 10)
    assert t.counts() == {"gemini": {"valid": 1, "total": 1}, "local": {"valid": 0, "total": 1}}


def test_dataset_uses_only_valid_teacher_decisions(tmp_path):
    f, _ = make(tmp_path, n_teacher=200, n_local=50)
    info = f.build_agent_dataset()
    assert info["train"] + info["holdout"] == 200 and info["holdout"] == 20
    lines = (tmp_path / "forge" / "datasets" / "agent-steps.jsonl").read_text().splitlines()
    msg = json.loads(lines[0])["messages"]
    assert [m["role"] for m in msg] == ["system", "user", "assistant"] and msg[0]["content"] == "SYS"
    assert f.datasets() == [{"name": "agent-steps", "examples": 180}]


def test_queue_needs_enough_data(tmp_path):
    f, _ = make(tmp_path, n_teacher=40)
    f.build_agent_dataset()
    with pytest.raises(ForgeError, match=f"at least {MIN_EXAMPLES}"):
        f.queue("agent", "agent-steps")
    with pytest.raises(ForgeError, match="No dataset"):
        f.queue("agent", "nope")


def test_window_and_tick(tmp_path, monkeypatch):
    f, _ = make(tmp_path, n_teacher=200)
    f.build_agent_dataset()
    assert f.in_window(datetime(2026, 11, 5, 2, 0)) and not f.in_window(datetime(2026, 11, 5, 12, 0))
    started = []
    monkeypatch.setattr(f, "run_job", lambda j: started.append(j["id"]))
    j = f.queue("agent", "agent-steps")
    monkeypatch.setattr(f, "in_window", lambda now=None: False)
    f.tick(); time.sleep(0.1)
    assert started == []  # waits for the window
    now_job = f.queue("urgent", "agent-steps", run_now=True)
    f.tick(); time.sleep(0.1)
    assert started == [now_job["id"]]
    assert f.cancel(j["id"]) and f.job(j["id"])["status"] == "cancelled" and not f.cancel(j["id"])


def test_run_job_hands_gpu_over_and_imports_model(tmp_path, monkeypatch):
    f, llm = make(tmp_path, n_teacher=200)
    f.build_agent_dataset()
    job = f.queue("agent", "agent-steps", run_now=True)
    gguf_dir = tmp_path / "forge" / "jobs" / job["id"] / "gguf"
    gguf_dir.mkdir(parents=True)
    (gguf_dir / "unsloth.Q4_K_M.gguf").write_bytes(b"GGUF-fake-weights")
    calls = []
    paused_during_training = []

    def post(url, json=None, content=None, timeout=None):
        calls.append(("POST", url.split(":11434")[-1].split(":8900")[-1], json))
        if url.endswith("/jobs"):
            paused_during_training.append(llm.paused.is_set())
            assert json["config"]["dataset"] == "/forge/datasets/agent-steps.jsonl"
            return Resp(status_code=202, text="", data={})
        return Resp(status_code=200, text="", data={})

    def get(url, timeout=None):
        if url.endswith("/api/ps"):
            return Resp(status_code=200, data={"models": [{"name": "gemma4:12b"}]})
        return Resp(status_code=200, data={"phase": "done", "loss": 0.42, "message": "done",
                                          "gguf": f"/forge/jobs/{job['id']}/gguf/unsloth.Q4_K_M.gguf"})

    monkeypatch.setattr("wetworks.forge.httpx.post", post)
    monkeypatch.setattr("wetworks.forge.httpx.get", get)
    monkeypatch.setattr("wetworks.forge.httpx.head", lambda url, timeout=None: Resp(status_code=404))
    monkeypatch.setattr("wetworks.forge.time.sleep", lambda s: None)
    done = f.run_job(job)
    assert done["status"] == "done" and done["model"].startswith("wetworks-agent:")
    assert paused_during_training == [True] and not llm.paused.is_set()  # paused while training, resumed after
    paths = [c[1] for c in calls]
    assert paths[0] == "/api/generate" and calls[0][2] == {"model": "gemma4:12b", "keep_alive": 0}  # unloaded first
    assert any(p.startswith("/api/blobs/sha256:") for p in paths)
    create = next(c for c in calls if c[1] == "/api/create")[2]
    assert create["model"] == done["model"] and list(create["files"]) == ["unsloth.Q4_K_M.gguf"]
    assert calls[-1][2] == {"model": "gemma4:12b", "keep_alive": -1}  # reloaded after


def test_failed_training_still_resumes_local_model(tmp_path, monkeypatch):
    f, llm = make(tmp_path, n_teacher=200)
    f.build_agent_dataset()
    job = f.queue("agent", "agent-steps", run_now=True)
    monkeypatch.setattr("wetworks.forge.httpx.post", lambda url, **k: Resp(status_code=202 if url.endswith("/jobs") else 200, text="", data={}))
    monkeypatch.setattr("wetworks.forge.httpx.get", lambda url, timeout=None: Resp(
        status_code=200, data={"models": []} if url.endswith("/api/ps") else {"phase": "failed", "message": "CUDA out of memory"}))
    monkeypatch.setattr("wetworks.forge.time.sleep", lambda s: None)
    done = f.run_job(job)
    assert done["status"] == "failed" and "CUDA out of memory" in done["message"] and not llm.paused.is_set()


def test_evaluate_scores_against_teacher(tmp_path, monkeypatch):
    f, llm = make(tmp_path, n_teacher=200)
    f.build_agent_dataset()
    answers = iter([json.dumps({"done": "x"}), "nonsense", json.dumps({"tool": "list_missions"}), json.dumps({"tool": "wrong"})] * 10)

    def post(url, json=None, timeout=None):
        return Resp(status_code=200, data={"message": {"content": next(answers)}})

    monkeypatch.setattr("wetworks.forge.httpx.post", post)
    res = f.evaluate("gemma4:12b", limit=8)
    assert res["n"] == 8 and 0 < res["valid"] <= 1 and res["same_tool"] <= res["same_action"] <= res["valid"]
    assert f.evals()[0]["model"] == "gemma4:12b"
    llm.paused.set()
    with pytest.raises(ForgeError, match="busy training"):
        f.evaluate("gemma4:12b")


def test_use_model_only_for_installed_models(tmp_path, monkeypatch):
    f, llm = make(tmp_path)
    monkeypatch.setattr("wetworks.forge.httpx.get", lambda url, timeout=None: Resp(
        status_code=200, data={"models": [{"name": "wetworks-agent:abc123", "size": 2.5e9}]}))
    with pytest.raises(ForgeError):
        f.use_model("not-there")
    assert "wetworks-agent:abc123" in f.use_model("wetworks-agent:abc123")
    assert llm.ollama.model == "wetworks-agent:abc123"
    assert (tmp_path / "forge" / "active_model.txt").read_text() == "wetworks-agent:abc123"


def test_local_model_paused_raises(tmp_path):
    from wetworks.ledger import Ledger
    from wetworks.llm import LLM, LocalModelPaused

    llm = LLM(Settings(dry_run=True, data_dir=tmp_path), Ledger(tmp_path / "l.db"))
    llm.paused.set()
    with pytest.raises(LocalModelPaused):
        llm.ask_local("x", "hi")


def test_runner_starts_jobs_and_reports_failure(tmp_path):
    """The real runner, in a subprocess: without Unsloth installed the job must fail cleanly."""
    import socket
    import urllib.request

    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    code = (f"import sys,runpy; sys.argv=['runner']; import http.server as h; "
            f"orig=h.ThreadingHTTPServer.__init__\n"
            f"def init(self, addr, handler): orig(self, ('127.0.0.1', {port}), handler)\n"
            f"h.ThreadingHTTPServer.__init__=init\n"
            f"runpy.run_path(r'{ROOT / 'forge' / 'runner.py'}', run_name='__main__')")
    env = {"FORGE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    proc = subprocess.Popen([sys.executable, "-c", code], env=env)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/health", timeout=1); break
            except Exception:
                time.sleep(0.1)
        ds = tmp_path / "d.jsonl"; ds.write_text('{"messages": []}\n')
        req = urllib.request.Request(base + "/jobs", data=json.dumps({"id": "j1", "config": {"dataset": str(ds), "base_model": "x"}}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        assert json.load(urllib.request.urlopen(req))["ok"]
        for _ in range(100):
            st = json.load(urllib.request.urlopen(base + "/jobs/j1"))
            if st["phase"] == "failed":
                break
            time.sleep(0.1)
        assert st["phase"] == "failed" and "unsloth" in st["message"].lower()
        bad = urllib.request.Request(base + "/jobs", data=b'{"id": "../x", "config": {}}', method="POST")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(bad)
    finally:
        proc.terminate()
