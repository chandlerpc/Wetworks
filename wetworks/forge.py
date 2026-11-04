"""The Forge (Wetworks side): datasets, training jobs, GPU handoff, Ollama import, benchmarks.

The GPU is shared: Ollama keeps the agents' local model loaded. Before training, the Forge
unloads it and pauses local thinking; afterwards it loads it back. Jobs run in the night window
(01:00-05:00 local) unless Patrick says run now.
"""

import hashlib
import json
import logging
import random
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .agent.runtime import parse_decision

log = logging.getLogger("wetworks.forge")

MIN_EXAMPLES = 150
TEACHER_BRAINS = ("gemini", "claude")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ForgeError(RuntimeError):
    pass


class Forge:
    def __init__(self, *, settings, llm, traces, events, data_dir: Path, runner_url: str, ollama_url: str):
        self.s = settings
        self.llm = llm
        self.traces = traces
        self.events = events
        self.dir = data_dir            # /data/forge in this container == /forge in the forge container
        self.runner = runner_url.rstrip("/")
        self.ollama = ollama_url.rstrip("/")
        (self.dir / "datasets").mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.dir / "forge.db", check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, created TEXT NOT NULL, updated TEXT NOT NULL, name TEXT NOT NULL,
                base_model TEXT NOT NULL, dataset TEXT NOT NULL, params TEXT NOT NULL, run_now INTEGER NOT NULL,
                status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS evals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, model TEXT NOT NULL, dataset TEXT NOT NULL,
                n INTEGER NOT NULL, valid REAL NOT NULL, same_action REAL NOT NULL, same_tool REAL NOT NULL);
        """)
        self.db.commit()
        self.working = False
        self.halt = None  # wetworks.halt.Halt

    # ---- datasets ------------------------------------------------------------------
    def build_agent_dataset(self, name: str = "agent-steps", holdout: float = 0.1) -> dict:
        """Decisions by the teacher brains -> chat-format JSONL for training, plus a held-out file for evals."""
        rows = self.traces.examples(TEACHER_BRAINS)
        random.Random(7).shuffle(rows)
        n_hold = max(1, int(len(rows) * holdout)) if len(rows) >= 20 else 0
        hold, train = rows[:n_hold], rows[n_hold:]

        def write(path: Path, items):
            with open(path, "w") as f:
                for r in items:
                    f.write(json.dumps({"messages": [{"role": "system", "content": r["system"]},
                                                     {"role": "user", "content": r["prompt"]},
                                                     {"role": "assistant", "content": r["output"]}]}) + "\n")

        write(self.dir / "datasets" / f"{name}.jsonl", train)
        write(self.dir / "datasets" / f"{name}.holdout.jsonl", hold)
        return {"name": name, "train": len(train), "holdout": len(hold), "needed": MIN_EXAMPLES}

    def datasets(self) -> list[dict]:
        out = []
        for p in sorted((self.dir / "datasets").glob("*.jsonl")):
            if p.name.endswith(".holdout.jsonl"):
                continue
            with open(p) as f:
                n = sum(1 for _ in f)
            out.append({"name": p.stem, "examples": n})
        return out

    # ---- jobs -----------------------------------------------------------------------
    KEYS = ("id", "created", "updated", "name", "base_model", "dataset", "params", "run_now", "status", "message", "model")

    def _row(self, r) -> dict:
        d = dict(zip(self.KEYS, r))
        d["params"] = json.loads(d["params"])
        d["run_now"] = bool(d["run_now"])
        return d

    def _set(self, jid: str, **kw) -> None:
        sets = ", ".join(f"{k} = ?" for k in kw)
        with self.lock:
            self.db.execute(f"UPDATE jobs SET {sets}, updated = ? WHERE id = ?", (*kw.values(), _now(), jid))
            self.db.commit()

    def queue(self, name: str, dataset: str, base_model: str = "", params: dict | None = None, run_now: bool = False) -> dict:
        path = self.dir / "datasets" / f"{dataset}.jsonl"
        if not path.exists():
            raise ForgeError(f"No dataset called {dataset!r}")
        with open(path) as f:
            n = sum(1 for _ in f)
        if n < MIN_EXAMPLES:
            raise ForgeError(f"Dataset {dataset!r} has {n} examples; at least {MIN_EXAMPLES} are needed for a useful run")
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name.lower())[:40] or "model"
        jid = f"{safe}-{uuid.uuid4().hex[:6]}"
        with self.lock:
            self.db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', '', '')",
                            (jid, _now(), _now(), safe, base_model or self.s.forge_base_model, dataset,
                             json.dumps(params or {}), int(run_now)))
            self.db.commit()
        self.events.add("forge", "info", f"Training job {jid} queued on {dataset} ({n} examples)"
                        + (" to run now" if run_now else " for tonight's window"))
        return self.job(jid)

    def job(self, jid: str) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
        return self._row(r) if r else None

    def jobs(self, limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def cancel(self, jid: str) -> bool:
        j = self.job(jid)
        if not j or j["status"] != "queued":
            return False
        self._set(jid, status="cancelled", message="Cancelled before it started")
        return True

    def in_window(self, now: datetime | None = None) -> bool:
        from zoneinfo import ZoneInfo

        hour = (now or datetime.now(ZoneInfo(self.s.timezone))).hour
        start, end = (int(x) for x in self.s.forge_window.split("-"))
        return start <= hour < end

    def tick(self) -> None:
        """Called every few minutes: start the next job if it's allowed to run now."""
        if self.working or (self.halt is not None and self.halt.is_set()):
            return
        with self.lock:
            rows = self.db.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY run_now DESC, created").fetchall()
        for r in rows:
            j = self._row(r)
            if j["run_now"] or self.in_window():
                threading.Thread(target=self.run_job, args=(j,), daemon=True).start()
                return

    # ---- GPU handoff ----------------------------------------------------------------
    def pause_local(self) -> None:
        self.llm.paused.set()
        try:
            loaded = httpx.get(f"{self.ollama}/api/ps", timeout=10).json().get("models", [])
            for m in loaded:
                httpx.post(f"{self.ollama}/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=60)
        except httpx.HTTPError as e:
            log.warning("Couldn't unload Ollama models: %s", e)
        time.sleep(5)

    def resume_local(self) -> None:
        self.llm.paused.clear()
        try:  # load the agents' model back so the first agent call isn't slow
            httpx.post(f"{self.ollama}/api/generate", json={"model": self.llm.ollama.model, "keep_alive": -1}, timeout=300)
        except httpx.HTTPError as e:
            log.warning("Couldn't reload the local model: %s", e)

    # ---- running a job --------------------------------------------------------------
    def run_job(self, j: dict, poll_seconds: int = 30, max_hours: float = 6) -> dict:
        jid = j["id"]
        self.working = True
        self.events.busy.add("forge")
        self.events.add("forge", "start", f"Training {jid}: pausing the local model and handing the GPU to the Forge")
        try:
            self.pause_local()
            cfg = {"base_model": j["base_model"], "dataset": f"/forge/datasets/{j['dataset']}.jsonl", **j["params"]}
            r = httpx.post(f"{self.runner}/jobs", json={"id": jid, "config": cfg}, timeout=30)
            if r.status_code != 202:
                raise ForgeError(f"The Forge refused the job ({r.status_code}): {r.text[:200]}")
            self._set(jid, status="running", message="Training")
            deadline = time.monotonic() + max_hours * 3600
            st = {}
            while time.monotonic() < deadline:
                time.sleep(poll_seconds)
                st = httpx.get(f"{self.runner}/jobs/{jid}", timeout=15).json()
                self._set(jid, message=str(st.get("message", ""))[:300])
                if st.get("phase") in ("done", "failed"):
                    break
            else:
                raise ForgeError(f"Training took longer than {max_hours} hours")
            if st.get("phase") != "done":
                raise ForgeError(st.get("message") or "Training failed")
            self._set(jid, status="importing", message="Registering the model in Ollama")
            gguf = Path(str(st["gguf"]).replace("/forge", str(self.dir), 1))
            model = f"wetworks-{j['name']}:{jid.rsplit('-', 1)[1]}"
            self.import_gguf(gguf, model)
            self._set(jid, status="done", message=f"Ready as {model} (loss {st.get('loss')})", model=model)
            self.events.add("forge", "done", f"Training {jid} finished: new local model {model}")
        except Exception as e:
            log.exception("Forge job %s failed", jid)
            self._set(jid, status="failed", message=f"{type(e).__name__}: {e}"[:300])
            self.events.add("forge", "error", f"Training {jid} failed: {e}"[:300])
        finally:
            self.resume_local()
            self.events.busy.discard("forge")
            self.working = False
        return self.job(jid)

    def import_gguf(self, path: Path, model: str) -> None:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        ref = f"sha256:{digest.hexdigest()}"
        if httpx.head(f"{self.ollama}/api/blobs/{ref}", timeout=30).status_code != 200:
            with open(path, "rb") as f:
                r = httpx.post(f"{self.ollama}/api/blobs/{ref}", content=f, timeout=1800)
            if r.status_code >= 300:
                raise ForgeError(f"Ollama wouldn't take the model file ({r.status_code})")
        r = httpx.post(f"{self.ollama}/api/create", json={"model": model, "files": {path.name: ref}, "stream": False},
                       timeout=1800)
        if r.status_code >= 300:
            raise ForgeError(f"Ollama couldn't create {model} ({r.status_code}): {r.text[:200]}")

    # ---- models and benchmarks ------------------------------------------------------
    def models(self) -> list[dict]:
        try:
            tags = httpx.get(f"{self.ollama}/api/tags", timeout=10).json().get("models", [])
        except Exception:
            return []
        return [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1)} for m in tags]

    def evaluate(self, model: str, dataset: str = "agent-steps", limit: int = 40) -> dict:
        """Score a local model on held-out agent decisions: valid step, same kind of action, same tool."""
        if self.llm.paused.is_set():
            raise ForgeError("The GPU is busy training; benchmark later")
        path = self.dir / "datasets" / f"{dataset}.holdout.jsonl"
        if not path.exists():
            raise ForgeError(f"No held-out set for {dataset!r}; build the dataset first")
        items = [json.loads(line) for line in open(path)][:limit]
        if not items:
            raise ForgeError("The held-out set is empty; collect more agent decisions first")
        self.events.busy.add("forge")
        try:
            valid = same_action = same_tool = 0
            for it in items:
                sys_msg, user, ref = (m["content"] for m in it["messages"])
                r = httpx.post(f"{self.ollama}/api/chat", json={
                    "model": model, "stream": False, "format": "json", "think": False,
                    "messages": [{"role": "system", "content": sys_msg}, {"role": "user", "content": user}],
                    "options": {"temperature": 0, "num_predict": 600}}, timeout=300)
                out = parse_decision((r.json().get("message") or {}).get("content", "")) if r.status_code == 200 else {}
                want = parse_decision(ref)
                if out and ("done" in out or out.get("tool")):
                    valid += 1
                    if ("done" in out) == ("done" in want):
                        same_action += 1
                        if "done" in out or out.get("tool") == want.get("tool"):
                            same_tool += 1
        finally:
            self.events.busy.discard("forge")
        n = len(items)
        res = {"model": model, "dataset": dataset, "n": n, "valid": round(valid / n, 3),
               "same_action": round(same_action / n, 3), "same_tool": round(same_tool / n, 3)}
        with self.lock:
            self.db.execute("INSERT INTO evals (ts, model, dataset, n, valid, same_action, same_tool) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (_now(), model, dataset, n, res["valid"], res["same_action"], res["same_tool"]))
            self.db.commit()
        self.events.add("forge", "done", f"Benchmark {model}: {res['valid']:.0%} valid steps, {res['same_tool']:.0%} matched the teacher")
        return res

    def evals(self, limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT ts, model, dataset, n, valid, same_action, same_tool FROM evals ORDER BY id DESC LIMIT ?",
                                   (limit,)).fetchall()
        return [dict(zip(("ts", "model", "dataset", "n", "valid", "same_action", "same_tool"), r)) for r in rows]

    def health(self) -> dict:
        try:
            return httpx.get(f"{self.runner}/health", timeout=5).json()
        except Exception as e:  # the Floor polls this; never let it fail
            return {"ok": False, "error": f"Forge container unreachable ({type(e).__name__})"}

    def use_model(self, model: str) -> str:
        """Switch the agents' local model (reversible: switch back the same way)."""
        names = {m["name"] for m in self.models()}
        if model not in names:
            raise ForgeError(f"{model} isn't in Ollama")
        old = self.llm.ollama.model
        self.llm.ollama.model = model
        (self.dir / "active_model.txt").write_text(model)
        self.events.add("forge", "info", f"Agents now think with {model} (was {old})")
        return f"Agents now use {model} (was {old}). Switch back any time."
