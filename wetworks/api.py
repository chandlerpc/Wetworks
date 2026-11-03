"""Command API and ops console. Private: reachable over Tailscale and protected by a token.

All interfaces (phone console, the ops-center UI, clAIr's bridge) talk to this one API.
"""

import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
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
               missions=None, open_mission=None) -> Starlette:
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
            mid = comms.post(channel, str(body.get("text", "")))
        except KeyError:
            return JSONResponse({"error": "Unknown channel."}, status_code=404)
        except ValueError:
            return JSONResponse({"error": "Write a message first."}, status_code=400)
        return JSONResponse({"id": mid}, status_code=202)

    async def action(request: Request):
        name = request.path_params["name"]
        if name == "intel" and "intel" in cells:
            if "intel" in events.busy:
                return JSONResponse({"error": "Intel is already running."}, status_code=409)
            run_bg(cells["intel"].safe_run)
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
            m = open_mission(str(body.get("title", "")), str(body.get("request", "")), "console")
        except ValueError:
            return JSONResponse({"error": "Describe what the mission should do."}, status_code=400)
        return JSONResponse(m, status_code=202)

    async def cancel_mission(request: Request):
        ok = missions.cancel(int(request.path_params["mid"])) if missions else False
        if not ok:
            return JSONResponse({"error": "Only queued or running missions can be cancelled."}, status_code=409)
        return JSONResponse({"ok": True}, status_code=202)

    async def index(request: Request):
        v = _asset_version()
        html = (CONSOLE_DIR / "index.html").read_text()
        for asset in ("console.css", "console.js", "manifest.json"):
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
        Route("/api/missions", list_missions, methods=["GET"]),
        Route("/api/missions", new_mission, methods=["POST"]),
        Route("/api/missions/{mid:int}", get_mission, methods=["GET"]),
        Route("/api/missions/{mid:int}/cancel", cancel_mission, methods=["POST"]),
        Mount("/static", FreshStatic(directory=CONSOLE_DIR), name="static"),
    ]
    return Starlette(routes=routes, middleware=[Middleware(TokenAuth, token=settings.console_token)])
