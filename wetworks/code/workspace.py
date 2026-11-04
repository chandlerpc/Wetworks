"""Git workspace for code missions.

Every mission gets the repo reset to the latest main and a fresh branch
`wetworks/mission-<id>`. Nothing is ever pushed to main; results go up as pull requests.
The GitHub token is passed per command as an HTTP header, so it's never written into the
clone's .git/config where model-written code could read it.
"""

import base64
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import httpx

# Paths a code mission may never touch.
BLOCKED_PATTERNS = [
    r"(^|/)\.git(/|$)",
    r"(^|/)\.github(/|$)",          # workflows run on push, before review, with repo secrets
    r"(^|/)\.env($|\.)",            # .env, .env.local... (but .env.example is allowed below)
    r"(^|/)\.ssh(/|$)",
    r"\.(pem|key|p12|pfx)$",
]
ALLOWED_EXCEPTIONS = [r"(^|/)\.env\.example$"]


class WorkspaceError(RuntimeError):
    pass


def safe_path(path: str) -> str:
    """Normalise a repo-relative path and refuse anything outside the repo or on the blocklist."""
    p = PurePosixPath(str(path).replace("\\", "/").lstrip("/"))
    if not str(p) or str(p) == "." or ".." in p.parts:
        raise WorkspaceError(f"Refusing path outside the repo: {path!r}")
    s = str(p)
    if any(re.search(a, s) for a in ALLOWED_EXCEPTIONS):
        return s
    if any(re.search(b, s) for b in BLOCKED_PATTERNS):
        raise WorkspaceError(f"Code missions can't edit {s}")
    return s


class Workspace:
    def __init__(self, root: Path, repo: str, token: str = "", remote_url: str | None = None,
                 api_base: str = "https://api.github.com"):
        """repo: "owner/name". remote_url overrides the GitHub URL (used by tests)."""
        self.repo = repo
        self.token = token
        self.dir = root / re.sub(r"[^A-Za-z0-9_.-]", "_", repo)
        self.remote = remote_url or f"https://github.com/{repo}.git"
        self.api_base = api_base.rstrip("/")
        self.base_branch = "main"

    # ---- git plumbing -------------------------------------------------------
    def _auth(self) -> list[str]:
        if not self.token or not self.remote.startswith("https://"):
            return []
        basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        return ["-c", f"http.extraHeader=Authorization: Basic {basic}"]

    def git(self, *args: str, cwd: Path | None = None, auth: bool = False, timeout: int = 300) -> str:
        # Hooks and fsmonitor can run commands; never let anything inside the repo trigger them.
        safety = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
        cmd = ["git", *safety, *(self._auth() if auth else []), *args]
        r = subprocess.run(cmd, cwd=cwd or self.dir, capture_output=True, text=True, timeout=timeout,
                           env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/usr/local/bin",
                                "HOME": str(self.dir.parent)})
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip().replace(self.token or "\x00", "***")
            raise WorkspaceError(f"git {args[0]} failed: {msg[-600:]}")
        return r.stdout

    def prepare(self, branch: str) -> None:
        """Fresh copy of the latest main, checked out on a new branch."""
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        if not (self.dir / ".git").exists():
            self.git("clone", "--depth", "50", self.remote, str(self.dir), cwd=self.dir.parent, auth=True)
        self.git("fetch", "--depth", "50", "origin", self.base_branch, auth=True)
        self.git("checkout", "-B", branch, f"origin/{self.base_branch}")
        self.git("reset", "--hard", f"origin/{self.base_branch}")
        self.git("clean", "-fdx")
        self.git("config", "user.name", "Wetworks Code cell")
        self.git("config", "user.email", "wetworks@users.noreply.github.com")

    # ---- reading ------------------------------------------------------------
    def tree(self, limit: int = 400) -> list[str]:
        files = self.git("ls-files").splitlines()
        return files[:limit]

    def read(self, path: str, max_chars: int = 20000) -> str:
        p = self.dir / safe_path(path)
        if not p.is_file():
            return ""
        return p.read_text(errors="replace")[:max_chars]

    # ---- writing ------------------------------------------------------------
    def apply(self, edits: list[dict]) -> list[str]:
        """Write full-file edits. Each edit: {"path": ..., "content": ...} or {"path": ..., "delete": true}."""
        changed = []
        for e in edits:
            rel = safe_path(e["path"])
            target = self.dir / rel
            if not target.resolve().is_relative_to(self.dir.resolve()):
                raise WorkspaceError(f"Refusing path outside the repo: {rel}")
            if e.get("delete"):
                if target.exists():
                    target.unlink()
                    changed.append(rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(e.get("content", "")))
            changed.append(rel)
        if changed:
            # Stage exactly these files now; later commits use only what's staged, so nothing a
            # test run writes can sneak into the pull request.
            self.git("add", "-A", "--", *changed)
        return changed

    def export(self, dest: Path) -> None:
        """Copy the working files (no .git) somewhere the sandbox can run them."""
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(self.dir, dest, ignore=shutil.ignore_patterns(".git"), symlinks=False)

    def diff_stat(self) -> str:
        return self.git("diff", "--cached", "--stat").strip()

    def staged(self) -> list[str]:
        return [p for p in self.git("diff", "--cached", "--name-only").splitlines() if p]

    def commit_and_push(self, branch: str, message: str) -> str:
        """Commit what's staged (only files the brain edited) and push the branch."""
        for path in self.staged():
            safe_path(path)  # belt and braces: nothing blocked may leave the machine
        if not self.staged():
            raise WorkspaceError("Nothing changed")
        self.git("commit", "-m", message)
        self.git("push", "--force-with-lease", "origin", f"HEAD:refs/heads/{branch}", auth=True)
        return self.git("rev-parse", "--short", "HEAD").strip()

    def open_pr(self, branch: str, title: str, body: str, draft: bool = False) -> str:
        """Open a pull request into main and return its URL."""
        if not self.token:
            raise WorkspaceError("No GitHub token, so the branch was pushed but no pull request was opened")
        r = httpx.post(
            f"{self.api_base}/repos/{self.repo}/pulls",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            json={"title": title[:200], "head": branch, "base": self.base_branch, "body": body[:60000],
                  "draft": draft},
            timeout=30,
        )
        if r.status_code == 422 and "already exists" in r.text:
            return f"https://github.com/{self.repo}/pulls"
        if r.status_code >= 300:
            raise WorkspaceError(f"GitHub wouldn't open the pull request ({r.status_code}): {r.text[:300]}")
        return r.json()["html_url"]
