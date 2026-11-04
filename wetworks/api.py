"""Command API and ops console. Private: reachable over Tailscale and protected by a token.

All interfaces (phone console, the ops-center UI, C.L.a.i.R's bridge) talk to this one API.
"""

import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .health import snapshot

CONSOLE_DIR = Path(__file__).parent / "console"


class FreshStatic(StaticFiles):
    """Static files that the browser must re-check every time, so updates show up immediately
    (home-screen apps on phones otherwise keep old copies for a long time)."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def _asset_version() -> str:
    return str(int(max(p.stat().st_mtime for p in CONSOLE_DIR.iterdir() if p.is_file())))


class TokenAuth(BaseHTTPMiddleware):
    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/"):
            header = request.headers.get("authorization", "")
            given = header[7:] if header.lower().startswith("bearer ") else ""
            if not (self.token and given and secrets.compare_digest(given, self.token)):
                return JSONResponse({"error": "Wrong or missing access code."}, status_code=401)
        return await call_next(request)


def _iso(dt) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def create_app(*, settings, architect, cells: dict, comms, events, scheduler=None,
               missions=None, open_mission=None, voice=None, leads=None, memory=None, approvals=None,
               commander=None, forge=None, halt=None, finance=None, dayjob=None) -> Starlette:
    def run_bg(fn):
        threading.Thread(target=fn, daemon=True).start()

    async def status(request: Request):
        mtd = architect.ledger.month_to_date()
        jobs = []
        if scheduler is not None:
            for j in scheduler.get_jobs():
                jobs.append({"id": j.id, "next_run": _iso(getattr(j, "next_run_time", None))})
            jobs.sort(key=lambda j: j["next_run"] or "")
        intel = cells.get("intel")
        return JSONResponse({
            "now": datetime.now(timezone.utc).isoformat(),
            "provider": settings.provider,
            "model": settings.local_model if settings.provider == "local" else settings.model_work,
            "spend": {"month_to_date": round(mtd, 4), "budget": settings.monthly_budget_usd},
            "host": snapshot(settings.data_dir),
            "busy": sorted(events.busy),
            "cells": [
                {"name": c.name, "kind": c.kind, "trial": c.in_trial(),
                 "topics": [t["name"] for t in getattr(c, "topics", [])]}
                for c in cells.values()
            ],
            "schedule": jobs,
            "intel_topics": len(intel.topics) if intel else 0,
            "clair": getattr(comms, "clair_mode", "local"),
        })

    async def get_events(request: Request):
        after = int(request.query_params.get("after", 0) or 0)
        return JSONResponse({"events": events.recent(after_id=after)})

    async def get_findings(request: Request):
        rows = architect.findings.db.execute(
            "SELECT ts, topic, title, summary, url, importance, reported FROM findings ORDER BY ts DESC LIMIT 60"
        ).fetchall()
        keys = ("ts", "topic", "title", "summary", "url", "importance", "reported")
        return JSONResponse({"findings": [dict(zip(keys, r)) for r in rows]})

    async def get_messages(request: Request):
        channel = request.path_params["channel"]
        after = int(request.query_params.get("after", 0) or 0)
        if channel not in ("architect", "clair"):
            return JSONResponse({"error": "Unknown channel."}, status_code=404)
        return JSONResponse({"messages": comms.messages(channel, after_id=after),
                             "typing": channel in events.busy})

    async def post_message(request: Request):
        channel = request.path_params["channel"]
        try:
            body = await request.json()
            wait = bool(body.get("wait"))  # voice waits for the reply so it can be spoken
            mid = await run_in_threadpool(comms.post, channel, str(body.get("text", "")), wait)
        except KeyError:
            return JSONResponse({"error": "Unknown channel."}, status_code=404)
        except ValueError:
            return JSONResponse({"error": "Write a message first."}, status_code=400)
        if wait:
            replies = [m for m in comms.messages(channel, after_id=mid) if m["sender"] == channel]
            return JSONResponse({"id": mid, "reply": replies[-1] if replies else None})
        return JSONResponse({"id": mid}, status_code=202)

    async def voice_config(request: Request):
        on = voice is not None
        return JSONResponse({
            "hear": bool(on and voice.can_hear),
            "speak": bool(on and voice.can_speak),
            "voice": settings.voice_name,
            "wake_phrase": settings.wake_phrase,
            "stop_phrases": [p.strip() for p in settings.stop_phrases.split("|") if p.strip()],
            "idle_seconds": settings.voice_idle_seconds,
            "chars_used": voice.chars_this_month() if on else 0,
            "chars_cap": settings.tts_monthly_char_cap,
        })

    async def voice_transcribe(request: Request):
        if voice is None or not voice.can_hear:
            return JSONResponse({"error": "Hearing isn't set up."}, status_code=503)
        audio = await request.body()
        if not audio:
            return JSONResponse({"error": "No audio received."}, status_code=400)
        if len(audio) > 12_000_000:
            return JSONResponse({"error": "That recording is too long."}, status_code=413)
        try:
            ctype = request.headers.get("content-type", "audio/webm")
            quick = request.query_params.get("quick") == "1"
            text = await run_in_threadpool(voice.transcribe, audio, ctype, quick)
        except Exception as e:
            events.add("clair", "warn", f"Couldn't transcribe: {e}")
            return JSONResponse({"error": "Couldn't make out the recording. Whisper may still be starting."}, status_code=502)
        if not quick and text:  # wake checks hear the whole room; only log what was meant for her
            events.add("clair", "info", f'Heard in {voice.last_ms / 1000:.1f}s: "{text[:120]}"')
        return JSONResponse({"text": text, "ms": voice.last_ms})

    async def voice_plan(request: Request):
        """Split a reply into chunks to voice one after another (the first plays sooner)."""
        from .voice import clean_for_speech, speech_chunks

        body = await request.json()
        return JSONResponse({"chunks": speech_chunks(clean_for_speech(str(body.get("text", ""))))})

    async def voice_say(request: Request):
        if voice is None:
            return JSONResponse({"error": "Voice isn't set up."}, status_code=503)
        body = await request.json()
        try:
            audio = await run_in_threadpool(voice.speak, str(body.get("text", "")))
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=503)
        return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})

    async def action(request: Request):
        name = request.path_params["name"]
        if name == "intel" and "intel" in cells:
            if "intel" in events.busy:
                return JSONResponse({"error": "Intel is already running."}, status_code=409)
            run_bg(cells["intel"].safe_run)
        elif name == "cycle" and commander is not None:
            if "architect" in events.busy:
                return JSONResponse({"error": "The Architect is busy. Try again in a moment."}, status_code=409)
            run_bg(lambda: commander.cycle("manual"))
        elif name == "finance" and "finance" in cells:
            run_bg(cells["finance"].safe_run)
        elif name == "revenue" and "revenue" in cells:
            if "revenue" in events.busy:
                return JSONResponse({"error": "Revenue is already hunting."}, status_code=409)
            run_bg(cells["revenue"].safe_run)
        elif name == "drill" and getattr(comms, "drills", None) is not None:
            ok, why = comms.drills.ready()
            if not ok:
                return JSONResponse({"error": f"Can't drill right now: {why}."}, status_code=409)
            run_bg(comms.drills.run_one)
        elif name in ("brief-morning", "brief-evening"):
            kind = name.split("-")[1]
            if "architect" in events.busy:
                return JSONResponse({"error": "The Architect is busy. Try again in a moment."}, status_code=409)
            run_bg(lambda: architect.brief(kind))
        else:
            return JSONResponse({"error": "Unknown action."}, status_code=404)
        return JSONResponse({"ok": True}, status_code=202)

    async def list_missions(request: Request):
        return JSONResponse({"missions": missions.recent() if missions else []})

    async def get_mission(request: Request):
        m = missions.get(int(request.path_params["mid"])) if missions else None
        if not m:
            return JSONResponse({"error": "No mission with that number."}, status_code=404)
        return JSONResponse(m)

    async def new_mission(request: Request):
        if not open_mission:
            return JSONResponse({"error": "Missions aren't available."}, status_code=503)
        try:
            body = await request.json()
            kind = "code" if body.get("kind") == "code" else "research"
            m = open_mission(str(body.get("title", "")), str(body.get("request", "")), "console", kind)
        except ValueError:
            return JSONResponse({"error": "Describe what the mission should do."}, status_code=400)
        return JSONResponse(m, status_code=202)

    async def cancel_mission(request: Request):
        ok = missions.cancel(int(request.path_params["mid"])) if missions else False
        if not ok:
            return JSONResponse({"error": "Only queued or running missions can be cancelled."}, status_code=409)
        return JSONResponse({"ok": True}, status_code=202)

    async def list_leads(request: Request):
        if leads is None:
            return JSONResponse({"leads": [], "counts": {}})
        status = request.query_params.get("status") or None
        rows = leads.due_followups(100) if status == "due" else leads.recent(status=status)
        return JSONResponse({"leads": rows, "counts": leads.counts(), "due": leads.due_count()})

    async def update_lead(request: Request):
        if leads is None:
            return JSONResponse({"error": "Leads aren't available."}, status_code=503)
        body = await request.json()
        rev = cells.get("revenue")
        try:
            lead = leads.update(int(request.path_params["lid"]), status=body.get("status"), notes=body.get("notes"),
                                followup_days=rev.next_followup_days(0) if rev else None)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if not lead:
            return JSONResponse({"error": "No lead with that number."}, status_code=404)
        if body.get("status"):
            events.add("revenue", "info", f"Lead marked {body['status']}: {lead['business']}")
        return JSONResponse(lead)

    async def redraft_lead(request: Request):
        lead = leads.get(int(request.path_params["lid"])) if leads is not None else None
        if not lead or "revenue" not in cells:
            return JSONResponse({"error": "No lead with that number."}, status_code=404)
        text = await run_in_threadpool(cells["revenue"].draft, lead)
        if not text:
            return JSONResponse({"error": "Couldn't write a draft just now. Try again in a minute."}, status_code=502)
        return JSONResponse(leads.get(lead["id"]))

    async def lead_action(request: Request):
        """followed-up | snooze | research. None of these contact the business."""
        lid, action = int(request.path_params["lid"]), request.path_params["action"]
        lead = leads.get(lid) if leads is not None else None
        rev = cells.get("revenue")
        if not lead or rev is None:
            return JSONResponse({"error": "No lead with that number."}, status_code=404)
        if action == "followed-up":
            if lead["status"] != "contacted":
                return JSONResponse({"error": "Mark the lead contacted first."}, status_code=409)
            nxt = rev.next_followup_days(lead["followups"] + 1)
            lead = leads.followed_up(lid, nxt)
            events.add("revenue", "info", f"Followed up with {lead['business']}"
                       + (f"; next nudge in {nxt} days" if nxt else "; that was the last follow-up"))
        elif action == "snooze":
            body = await request.json() if (await request.body()) else {}
            days = max(1, min(30, int(body.get("days", 3))))
            if lead["status"] != "contacted":
                return JSONResponse({"error": "Only contacted leads have follow-ups."}, status_code=409)
            lead = leads.snooze(lid, days)
        elif action == "research":
            if lead["research_mission"] and not lead["research"]:
                return JSONResponse({"error": f"Research is already running (mission #{lead['research_mission']})."},
                                    status_code=409)
            leads.set_research(lid, text="")
            m = await run_in_threadpool(rev.research, leads.get(lid))
            if not m:
                return JSONResponse({"error": "Missions aren't available."}, status_code=503)
            lead = leads.get(lid)
        else:
            return JSONResponse({"error": "Unknown action."}, status_code=404)
        return JSONResponse(lead)

    # ---- Finance (local only; this is Patrick's money) ----------------------------------------
    async def finance_overview(request: Request):
        if finance is None:
            return JSONResponse({"error": "Finance isn't set up."}, status_code=503)
        from .finance import CATEGORIES

        month = request.query_params.get("month") or None
        return JSONResponse({**finance.summary_for("local"), "month_detail": finance.month(month), "categories": CATEGORIES,
                             "imports": finance.batches(), "count": finance.count(),
                             "uncategorized": len(finance.uncategorized_merchants(500)),
                             "wetworks_ai": round(architect.ledger.month_to_date(), 2)})

    async def finance_tx(request: Request):
        if finance is None:
            return JSONResponse({"transactions": []})
        q = request.query_params
        return JSONResponse({"transactions": finance.transactions(q.get("category", ""), q.get("q", ""), q.get("month", ""),
                                                                   int(q.get("limit", 200)))})

    async def finance_import(request: Request):
        if finance is None:
            return JSONResponse({"error": "Finance isn't set up."}, status_code=503)
        from .finance import FinanceError

        raw = await request.body()
        if not raw:
            return JSONResponse({"error": "No file received."}, status_code=400)
        if len(raw) > 8_000_000:
            return JSONResponse({"error": "That file is too big for one import."}, status_code=413)
        q = request.query_params
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        try:
            r = await run_in_threadpool(finance.import_csv, text, q.get("account", ""), q.get("filename", ""), q.get("flip", "auto"))
        except FinanceError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        events.add("finance", "info", f"Imported {r['added']} transactions into {r['account']}"
                   + (f" ({r['skipped']} already there)" if r["skipped"] else ""))
        if "finance" in cells:
            run_bg(cells["finance"].sort_uncategorized)
        return JSONResponse(r)

    async def finance_undo(request: Request):
        n = finance.undo_batch(int(request.path_params["bid"])) if finance is not None else 0
        events.add("finance", "info", f"Removed an import ({n} transactions)")
        return JSONResponse({"removed": n})

    async def finance_set(request: Request):
        from .finance import FinanceError

        body = await request.json()
        try:
            t = finance.set_category(int(request.path_params["tid"]), str(body.get("category", "")), bool(body.get("remember", True)))
        except FinanceError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if not t:
            return JSONResponse({"error": "No such transaction."}, status_code=404)
        return JSONResponse(t)

    # ---- Day Job (local only; work material, scrubbed of patient details) ---------------------
    async def dayjob_overview(request: Request):
        if dayjob is None:
            return JSONResponse({"error": "Day Job isn't set up."}, status_code=503)
        from .dayjob import PRODUCTS

        status = request.query_params.get("status", "")
        return JSONResponse({"counts": dayjob.counts(), "tickets": dayjob.tickets(status), "fixes": dayjob.fixes(50),
                             "docs": dayjob.docs(), "products": PRODUCTS})

    async def dayjob_new_ticket(request: Request):
        if dayjob is None:
            return JSONResponse({"error": "Day Job isn't set up."}, status_code=503)
        body = await request.json()
        try:
            t, found = dayjob.new_ticket(str(body.get("text", "")), str(body.get("product", "Other")), str(body.get("ref", "")))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        events.add("dayjob", "info", f"New ticket #{t['id']} ({t['product']})"
                   + (f"; removed {sum(found.values())} patient detail(s)" if found else ""))
        if body.get("help", True) and "dayjob" in cells:
            run_bg(lambda: cells["dayjob"].help(t["id"]))
        return JSONResponse({"ticket": t, "removed": found})

    async def dayjob_ticket(request: Request):
        if dayjob is None:
            return JSONResponse({"error": "Day Job isn't set up."}, status_code=503)
        tid = int(request.path_params["tid"])
        if request.method == "GET":
            t = dayjob.ticket(tid)
            return JSONResponse(t) if t else JSONResponse({"error": "No such ticket."}, status_code=404)
        body = await request.json()
        try:
            if body.get("action") == "help" and "dayjob" in cells:
                run_bg(lambda: cells["dayjob"].help(tid))
                return JSONResponse({"ok": True}, status_code=202)
            if body.get("status") == "solved":
                t = dayjob.solve(tid, str(body.get("resolution", "")), str(body.get("title", "")), bool(body.get("save_fix", True)))
                if t:
                    events.add("dayjob", "done", f"Ticket #{tid} solved" + (" and saved as a fix" if t["fix_id"] else ""))
            else:
                t = dayjob.update_ticket(tid, status=body.get("status"))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(t) if t else JSONResponse({"error": "No such ticket."}, status_code=404)

    async def dayjob_fix(request: Request):
        if dayjob is None:
            return JSONResponse({"error": "Day Job isn't set up."}, status_code=503)
        b = await request.json()
        try:
            f = dayjob.add_fix(str(b.get("title", "")), str(b.get("symptoms", "")), str(b.get("steps", "")), str(b.get("product", "Other")))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(f)

    async def dayjob_doc(request: Request):
        if dayjob is None:
            return JSONResponse({"error": "Day Job isn't set up."}, status_code=503)
        from .dayjob import read_upload

        if request.method == "DELETE":
            return JSONResponse({"ok": dayjob.delete_doc(int(request.path_params["did"]))})
        raw = await request.body()
        name = request.query_params.get("name", "notes.txt")
        if not raw or len(raw) > 15_000_000:
            return JSONResponse({"error": "Send a file under 15 MB."}, status_code=400)
        try:
            text = await run_in_threadpool(read_upload, name, raw)
            d = await run_in_threadpool(dayjob.add_doc, name, text)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        events.add("dayjob", "info", f"Added {name} to the Day Job library ({d['chunks']} sections)")
        return JSONResponse(d)

    async def dayjob_search(request: Request):
        q = request.query_params.get("q", "")
        return JSONResponse({"results": [{k: h[k] for k in ("kind", "ref", "title", "snippet")} for h in dayjob.search(q, 12)]
                             if dayjob is not None else []})

    async def list_memory(request: Request):
        if memory is None:
            return JSONResponse({"memories": [], "count": 0})
        q = request.query_params.get("q", "").strip()
        rows = memory.search(q, limit=50, include_pinned=True) if q else memory.recent(200)
        return JSONResponse({"memories": rows, "count": memory.count()})

    async def add_memory(request: Request):
        if memory is None:
            return JSONResponse({"error": "Memory isn't available."}, status_code=503)
        body = await request.json()
        m = memory.add(str(body.get("text", "")), kind=str(body.get("kind", "fact")), source="console")
        if not m:
            return JSONResponse({"error": "That's empty or already remembered."}, status_code=400)
        events.add("architect", "info", f"Remembered: {m['text'][:120]}")
        return JSONResponse(m, status_code=201)

    async def forget_memory(request: Request):
        if memory is None or not memory.forget(int(request.path_params["mid"])):
            return JSONResponse({"error": "Nothing to forget with that number."}, status_code=404)
        return JSONResponse({"ok": True})

    async def list_approvals(request: Request):
        if approvals is None:
            return JSONResponse({"pending": [], "recent": []})
        return JSONResponse({"pending": approvals.pending(), "recent": approvals.recent(10)})

    async def decide_approval(request: Request):
        if approvals is None:
            return JSONResponse({"error": "Approvals aren't available."}, status_code=503)
        body = await request.json()
        decision = body.get("decision")
        if decision not in ("approve", "decline"):
            return JSONResponse({"error": "Choose approve or decline."}, status_code=400)
        try:
            a = await run_in_threadpool(approvals.decide, int(request.path_params["aid"]), decision == "approve")
        except KeyError:
            return JSONResponse({"error": "No request with that number."}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        events.add("architect", "info" if a["status"] != "failed" else "error",
                   f"Approval #{a['id']} {a['status']}: {a['summary']}" + (f" ({a['result'][:120]})" if a["result"] else ""))
        return JSONResponse(a)

    async def forge_status(request: Request):
        if forge is None:
            return JSONResponse({"error": "The Forge isn't set up."}, status_code=503)
        def gather():
            return {"forge": forge.health(), "datasets": forge.datasets(), "jobs": forge.jobs(10), "models": forge.models(),
                    "evals": forge.evals(10), "decisions": forge.traces.counts(), "agents_use": forge.llm.ollama.model,
                    "window": settings.forge_window, "base_model": settings.forge_base_model,
                    "training": forge.llm.paused.is_set()}
        return JSONResponse(await run_in_threadpool(gather))

    async def forge_action(request: Request):
        if forge is None:
            return JSONResponse({"error": "The Forge isn't set up."}, status_code=503)
        name = request.path_params["name"]
        body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
        try:
            if name == "dataset":
                return JSONResponse(await run_in_threadpool(forge.build_agent_dataset))
            if name == "train":
                return JSONResponse(forge.queue(str(body.get("name") or "agent"), str(body.get("dataset") or "agent-steps"),
                                                run_now=bool(body.get("now"))), status_code=202)
            if name == "eval":
                if "forge" in events.busy:
                    return JSONResponse({"error": "The Forge is busy."}, status_code=409)
                run_bg(lambda: forge.evaluate(str(body.get("model")), str(body.get("dataset") or "agent-steps")))
                return JSONResponse({"ok": True}, status_code=202)
            if name == "use":
                return JSONResponse({"message": await run_in_threadpool(forge.use_model, str(body.get("model")))})
            if name == "cancel":
                ok = forge.cancel(str(body.get("id")))
                return JSONResponse({"ok": ok}, status_code=200 if ok else 409)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"error": "Unknown Forge action."}, status_code=404)

    async def floor(request: Request):
        from .floor import floor_state

        state = await run_in_threadpool(lambda: floor_state(
            settings=settings, architect=architect, comms=comms, events=events, scheduler=scheduler, missions=missions,
            approvals=approvals, forge=forge, leads=getattr(comms, "leads", None), halt=halt,
            finance=finance, dayjob=dayjob))
        return JSONResponse(state)

    async def set_halt(request: Request):
        if halt is None:
            return JSONResponse({"error": "The kill switch isn't available."}, status_code=503)
        body = await request.json()
        on = body.get("halted")
        if not isinstance(on, bool):
            return JSONResponse({"error": "Send halted: true or false."}, status_code=400)
        if on and body.get("confirm") != "HALT":
            return JSONResponse({"error": "Confirm the halt."}, status_code=400)
        halt.set(on)
        events.add("architect", "warn" if on else "info",
                   "KILL SWITCH: all autonomous work halted by Patrick" if on else "Kill switch released: work resumes")
        if not on and getattr(comms, "operator", None) is not None:
            comms.operator.submit()  # pick queued missions back up
        return JSONResponse({"halted": halt.is_set()})

    async def index(request: Request):
        v = _asset_version()
        html = (CONSOLE_DIR / "index.html").read_text()
        for asset in ("console.css", "console.js", "voice.js", "floor.js", "manifest.json"):
            html = html.replace(f"/static/{asset}", f"/static/{asset}?v={v}")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    routes = [
        Route("/", index),
        Route("/api/status", status),
        Route("/api/events", get_events),
        Route("/api/findings", get_findings),
        Route("/api/comms/{channel}", get_messages, methods=["GET"]),
        Route("/api/comms/{channel}", post_message, methods=["POST"]),
        Route("/api/actions/{name}", action, methods=["POST"]),
        Route("/api/voice", voice_config, methods=["GET"]),
        Route("/api/voice/transcribe", voice_transcribe, methods=["POST"]),
        Route("/api/voice/say", voice_say, methods=["POST"]),
        Route("/api/voice/plan", voice_plan, methods=["POST"]),
        Route("/api/floor", floor, methods=["GET"]),
        Route("/api/halt", set_halt, methods=["POST"]),
        Route("/api/forge", forge_status, methods=["GET"]),
        Route("/api/forge/{name}", forge_action, methods=["POST"]),
        Route("/api/finance", finance_overview, methods=["GET"]),
        Route("/api/finance/transactions", finance_tx, methods=["GET"]),
        Route("/api/finance/transactions/{tid:int}", finance_set, methods=["POST"]),
        Route("/api/finance/import", finance_import, methods=["POST"]),
        Route("/api/finance/imports/{bid:int}", finance_undo, methods=["DELETE"]),
        Route("/api/dayjob", dayjob_overview, methods=["GET"]),
        Route("/api/dayjob/tickets", dayjob_new_ticket, methods=["POST"]),
        Route("/api/dayjob/tickets/{tid:int}", dayjob_ticket, methods=["GET", "POST"]),
        Route("/api/dayjob/fixes", dayjob_fix, methods=["POST"]),
        Route("/api/dayjob/docs", dayjob_doc, methods=["POST"]),
        Route("/api/dayjob/docs/{did:int}", dayjob_doc, methods=["DELETE"]),
        Route("/api/dayjob/search", dayjob_search, methods=["GET"]),
        Route("/api/approvals", list_approvals, methods=["GET"]),
        Route("/api/approvals/{aid:int}", decide_approval, methods=["POST"]),
        Route("/api/memory", list_memory, methods=["GET"]),
        Route("/api/memory", add_memory, methods=["POST"]),
        Route("/api/memory/{mid:int}", forget_memory, methods=["DELETE"]),
        Route("/api/leads", list_leads, methods=["GET"]),
        Route("/api/leads/{lid:int}", update_lead, methods=["POST"]),
        Route("/api/leads/{lid:int}/draft", redraft_lead, methods=["POST"]),
        Route("/api/leads/{lid:int}/{action:str}", lead_action, methods=["POST"]),
        Route("/api/missions", list_missions, methods=["GET"]),
        Route("/api/missions", new_mission, methods=["POST"]),
        Route("/api/missions/{mid:int}", get_mission, methods=["GET"]),
        Route("/api/missions/{mid:int}/cancel", cancel_mission, methods=["POST"]),
        Mount("/static", FreshStatic(directory=CONSOLE_DIR), name="static"),
    ]
    return Starlette(routes=routes, middleware=[Middleware(TokenAuth, token=settings.console_token)])
