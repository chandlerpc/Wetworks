"""Wetworks test sandbox.

Runs a project's tests on a copy of the files (never the real repo) with no secrets and no
internet. Only reachable from The Architect over an internal Docker network.

POST /run  {"run": "<folder under /runs>"}  ->  {"exit_code", "timed_out", "output"}
"""

import json
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RUNS = Path(os.environ.get("RUNS_DIR", "/runs"))
TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "300"))
MAX_OUTPUT = 12000


def run_tests(name: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name or ""):
        return {"error": "bad run name"}
    folder = RUNS / name
    if not folder.is_dir():
        return {"error": "no such run"}
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": str(folder), "WETWORKS_DRY_RUN": "true", "WETWORKS_DATA_DIR": "/tmp/wetworks-data"}
    cmd = ["python", "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"]
    try:
        r = subprocess.run(cmd, cwd=folder, env=env, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired as e:
        partial = "".join(x if isinstance(x, str) else (x or b"").decode(errors="replace") for x in (e.stdout, e.stderr))
        return {"exit_code": -1, "timed_out": True,
                "output": (partial + f"\n[stopped after {TIMEOUT}s]")[-MAX_OUTPUT:]}
    return {"exit_code": r.returncode, "timed_out": False, "output": (r.stdout + r.stderr)[-MAX_OUTPUT:]}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/run":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        result = run_tests(str(body.get("run", "")))
        self._send(400 if "error" in result else 200, result)

    def do_GET(self):
        self._send(200, {"ok": True}) if self.path == "/health" else self._send(404, {"error": "not found"})

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8800), Handler).serve_forever()
