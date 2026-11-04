import subprocess
from types import SimpleNamespace

import pytest

from wetworks.code.workspace import Workspace, WorkspaceError, safe_path


def make_origin(tmp_path):
    """A bare repo with one commit on main, standing in for GitHub."""
    seed = tmp_path / "seed"
    seed.mkdir()
    run = lambda *a, cwd=seed: subprocess.run(a, cwd=cwd, check=True, capture_output=True)
    run("git", "init", "-q", "-b", "main")
    (seed / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (seed / "README.md").write_text("# demo\n")
    run("git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    origin = tmp_path / "origin.git"
    run("git", "clone", "-q", "--bare", str(seed), str(origin), cwd=tmp_path)
    return origin


@pytest.mark.parametrize("path,ok", [
    ("app.py", True), ("src/new/mod.py", True), (".env.example", True), ("/app.py", True),
    ("../etc/passwd", False), (".git/config", False), (".github/workflows/ci.yml", False),
    (".env", False), ("config/.env.local", False), ("keys/server.pem", False), ("", False),
])
def test_safe_path(path, ok):
    if ok:
        assert safe_path(path)
    else:
        with pytest.raises(WorkspaceError):
            safe_path(path)


def test_branch_edit_commit_push(tmp_path):
    origin = make_origin(tmp_path)
    ws = Workspace(tmp_path / "ws", "chandlerpc/demo", remote_url=str(origin))
    ws.prepare("wetworks/mission-1")
    assert "app.py" in ws.tree() and "return a + b" in ws.read("app.py")

    changed = ws.apply([{"path": "app.py", "content": "def add(a, b):\n    return a + b + 0\n"},
                        {"path": "tests/test_app.py", "content": "from app import add\n"},
                        {"path": "README.md", "delete": True}])
    assert changed == ["app.py", "tests/test_app.py", "README.md"]
    assert "3 files changed" in ws.diff_stat()
    sha = ws.commit_and_push("wetworks/mission-1", "Mission 1")
    branches = subprocess.run(["git", "branch"], cwd=origin, capture_output=True, text=True).stdout
    assert "wetworks/mission-1" in branches and sha

    # main on the "remote" is untouched
    main_log = subprocess.run(["git", "log", "--oneline", "main"], cwd=origin, capture_output=True, text=True).stdout
    assert main_log.count("\n") == 1

    # next mission starts clean from main again
    ws.prepare("wetworks/mission-2")
    assert (ws.dir / "README.md").exists() and not (ws.dir / "tests").exists()
    with pytest.raises(WorkspaceError, match="Nothing changed"):
        ws.commit_and_push("wetworks/mission-2", "noop")


def test_apply_refuses_blocked_paths(tmp_path):
    ws = Workspace(tmp_path / "ws", "o/r", remote_url=str(make_origin(tmp_path)))
    ws.prepare("b")
    with pytest.raises(WorkspaceError, match="can't edit"):
        ws.apply([{"path": ".github/workflows/x.yml", "content": "on: push"}])


def test_token_is_not_stored_in_git_config(tmp_path):
    origin = make_origin(tmp_path)
    ws = Workspace(tmp_path / "ws", "o/r", token="ghp_secret", remote_url=str(origin))
    assert ws._auth() == []  # never sent to non-https remotes
    ws.prepare("b")
    assert "ghp_secret" not in (ws.dir / ".git" / "config").read_text()
    ws.remote = "https://github.com/o/r.git"
    header = " ".join(ws._auth())
    assert "Authorization: Basic" in header and "ghp_secret" not in header  # base64, per command only


def test_open_pr(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / "ws", "chandlerpc/Wetworks", token="t")
    sent = {}

    def post(url, headers, json, timeout):
        sent.update(url=url, body=json, auth=headers["Authorization"])
        return SimpleNamespace(status_code=201, text="", json=lambda: {"html_url": "https://github.com/chandlerpc/Wetworks/pull/7"})

    monkeypatch.setattr("wetworks.code.workspace.httpx.post", post)
    assert ws.open_pr("wetworks/mission-3", "Add thing", "body") == "https://github.com/chandlerpc/Wetworks/pull/7"
    assert sent["url"].endswith("/repos/chandlerpc/Wetworks/pulls") and sent["body"]["base"] == "main"
    assert sent["body"]["head"] == "wetworks/mission-3"
    with pytest.raises(WorkspaceError, match="No GitHub token"):
        Workspace(tmp_path / "w2", "o/r").open_pr("b", "t", "b")
