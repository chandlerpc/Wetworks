"""Code Splinter — works on the Wetworks repo (or another configured repo) for one mission.

  1. Fresh branch `wetworks/mission-<id>` from the latest main.
  2. Brain reads the file list and picks what it needs, then proposes full-file edits.
  3. Edits are staged; a copy of the files (no .git) runs its tests in the sandbox
     (no secrets, no internet). One repair round if they fail.
  4. Commit only the staged files, push the branch, open a pull request (draft if tests fail).
Nothing is ever merged automatically.
"""

import shutil
import uuid

import httpx

from ..code.workspace import WorkspaceError
from .splinter import Cancelled, MissionResult


class Coder:
    name = "coder"
    kind = "splinter"

    def __init__(self, ctx, store, workspace, brain, runs_dir, sandbox_url: str, repair_rounds: int = 1):
        self.ctx = ctx
        self.store = store
        self.ws = workspace
        self.brain = brain
        self.runs_dir = runs_dir
        self.sandbox_url = sandbox_url.rstrip("/")
        self.repair_rounds = repair_rounds

    def _step(self, mid: int, text: str) -> None:
        self.store.add_step(mid, text)
        if self.ctx.events is not None:
            self.ctx.events.add("splinter", "info", f"Mission #{mid}: {text}")

    def _check(self, mid: int) -> None:
        if mid in self.store.cancel_requested:
            raise Cancelled()

    def run_tests(self, mid: int) -> dict:
        """Copy the files into the sandbox area and run the tests there."""
        run = f"m{mid}-{uuid.uuid4().hex[:8]}"
        dest = self.runs_dir / run
        try:
            self.ws.export(dest)
            r = httpx.post(f"{self.sandbox_url}/run", json={"run": run}, timeout=360)
            if r.status_code != 200:
                return {"ran": False, "passed": False, "output": f"Sandbox refused the run ({r.status_code})"}
            data = r.json()
            return {"ran": True, "passed": data.get("exit_code") == 0, "output": data.get("output", ""),
                    "timed_out": data.get("timed_out", False)}
        except httpx.HTTPError as e:
            return {"ran": False, "passed": False, "output": f"Sandbox unreachable ({type(e).__name__})"}
        finally:
            shutil.rmtree(dest, ignore_errors=True)

    def run(self, mission: dict) -> MissionResult:
        mid = mission["id"]
        branch = f"wetworks/mission-{mid}"
        task = mission["request"]
        agent = f"code:{mid}"

        self._step(mid, f"Checking out a fresh copy of {self.ws.repo} on {branch}")
        self.ws.prepare(branch)
        tree = self.ws.tree()
        self._check(mid)

        self._step(mid, f"Planning with the {self.brain.name} brain")
        plan = self.brain.plan(agent, task, tree, self.ws.read("README.md", 4000))
        files = {p: self.ws.read(p) for p in plan["files"]}
        self._step(mid, f"Reading {len(files)} file(s): {', '.join(files) or 'none'}")
        self._check(mid)

        summary, tests, feedback = "", {"ran": False, "passed": False, "output": ""}, ""
        for attempt in range(1 + self.repair_rounds):
            self._step(mid, "Writing the change" if attempt == 0 else "Fixing the failing tests")
            change = self.brain.edit(agent, task, plan["approach"], files, feedback=feedback)
            if not change["edits"]:
                raise RuntimeError("The brain didn't propose any file changes. Try describing the task in more detail.")
            changed = self.ws.apply(change["edits"])
            summary = change["summary"] or summary
            for p in changed:  # show the brain its own latest version on a repair round
                files[p] = self.ws.read(p)
            self._step(mid, f"Changed {len(changed)} file(s): {', '.join(changed)}")
            self._check(mid)

            self._step(mid, "Running the tests in the sandbox")
            tests = self.run_tests(mid)
            if tests["passed"] or not tests["ran"]:
                break
            feedback = tests["output"]
            self._step(mid, "Tests failed" + (", trying one fix" if attempt < self.repair_rounds else ""))
            self._check(mid)

        status = "passed" if tests["passed"] else ("didn't run" if not tests["ran"] else "failing")
        stat = self.ws.diff_stat()
        message = f"Mission #{mid}: {mission['title']}\n\n{summary}\n\nTests: {status}. Brain: {self.brain.name}."
        try:
            self._step(mid, "Pushing the branch")
            self.ws.commit_and_push(branch, message)
            pr = self.ws.open_pr(
                branch,
                f"[Wetworks] {mission['title']}",
                (f"**Request**\n\n{task}\n\n**What changed**\n\n{summary or '(no summary)'}\n\n"
                 f"**Files**\n\n```\n{stat}\n```\n\n**Tests:** {status}\n\n"
                 + (f"<details><summary>Test output</summary>\n\n```\n{tests['output'][-4000:]}\n```\n</details>\n\n" if tests["output"] else "")
                 + f"_Opened by the Wetworks Code cell (mission #{mid}, {self.brain.name} brain). Review before merging; nothing merges automatically._"),
                draft=not tests["passed"],
            )
        except WorkspaceError as e:
            text = (f"{summary}\n\nThe change is ready on a local branch but couldn't be published: {e}\n\n"
                    f"Files:\n{stat}\n\nTests: {status}.\n\nNext step: add a GitHub token so I can open pull requests.")
            return MissionResult(text=text, sources=[])

        verdict = "Tests pass" if tests["passed"] else f"Tests {status}, so it's opened as a draft"
        text = (f"## What changed\n{summary or '(no summary)'}\n\n## Files\n{stat}\n\n## Tests\n{verdict}.\n\n"
                f"Next step: review and merge the pull request [1] if it looks right.")
        return MissionResult(text=text, sources=[{"n": 1, "title": f"Pull request: {mission['title']}", "url": pr}])
