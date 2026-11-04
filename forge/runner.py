"""Forge job runner. Runs one training job at a time inside the GPU container.

POST /jobs   {"id": "<job id>", "config": {...}}   start a job (409 if one is running)
GET  /jobs/<id>                                      status.json for that job
GET  /health                                         {"ok": true, "gpu": "...", "running": "<id or null>"}
"""

import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("FORGE_DIR", "/forge")
JOBS = os.path.join(ROOT, "jobs")
TRAIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "train.py")
current = {"id": None, "proc": None}


def gpu_name() -> str:
    try:
        return subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as e:
        return f"unavailable ({type(e).__name__})"


def gpu_stats() -> dict:
    """Live load, temperature and memory for the ops-center Floor."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout
        util, temp, used, total = (float(x) for x in out.strip().split("\n")[0].split(","))
        return {"gpu_util": util, "gpu_temp": temp, "gpu_mem_used": used, "gpu_mem_total": total}
    except Exception:
        return {}


def running() -> str | None:
    p = current["proc"]
    if p is not None and p.poll() is None:
        return current["id"]
    return None


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "gpu": gpu_name(), "running": running(), **gpu_stats()})
        m = re.fullmatch(r"/jobs/([A-Za-z0-9_-]{1,80})", self.path)
        if not m:
            return self._send(404, {"error": "not found"})
        path = os.path.join(JOBS, m.group(1), "status.json")
        if not os.path.exists(path):
            return self._send(404, {"error": "no such job"})
        with open(path) as f:
            st = json.load(f)
        if st.get("phase") not in ("done", "failed") and running() != m.group(1):
            st["phase"], st["message"] = "failed", "The training process stopped unexpectedly (out of memory?)"
        return self._send(200, st)

    def do_POST(self):
        if self.path != "/jobs":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        jid = str(body.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", jid):
            return self._send(400, {"error": "bad job id"})
        if running():
            return self._send(409, {"error": f"job {running()} is still running"})
        cfg = body.get("config") or {}
        if not os.path.exists(str(cfg.get("dataset", ""))):
            return self._send(400, {"error": "dataset file not found in the forge volume"})
        jd = os.path.join(JOBS, jid)
        os.makedirs(jd, exist_ok=True)
        with open(os.path.join(jd, "config.json"), "w") as f:
            json.dump(cfg, f)
        with open(os.path.join(jd, "status.json"), "w") as f:
            json.dump({"phase": "starting", "message": "Starting"}, f)
        log = open(os.path.join(jd, "train.log"), "w")
        current["id"], current["proc"] = jid, subprocess.Popen([sys.executable, TRAIN, os.path.join(jd, "config.json")],
                                                              stdout=log, stderr=subprocess.STDOUT)
        return self._send(202, {"ok": True, "id": jid})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    os.makedirs(JOBS, exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", 8900), Handler).serve_forever()
