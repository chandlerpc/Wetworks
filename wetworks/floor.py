"""State of the ops-center Floor: every agent, where it is, what it's doing, and the HUD numbers.
The UI only reads this; it never drives the work."""

from datetime import datetime, timedelta, timezone

ROOMS = [
    {"id": "command", "name": "Command Deck", "sealed": False},
    {"id": "comms", "name": "Comms Booth", "sealed": False},
    {"id": "intel", "name": "Intel Watch", "sealed": False},
    {"id": "revenue", "name": "Revenue Room", "sealed": False},
    {"id": "ready", "name": "Splinter Ready Room", "sealed": False},
    {"id": "codebay", "name": "Code Bay", "sealed": False},
    {"id": "forge", "name": "The Forge", "sealed": False},
    {"id": "archives", "name": "Archives", "sealed": False},
    {"id": "dayjob", "name": "Day Job", "sealed": False},
    {"id": "finance", "name": "Finance", "sealed": False},
    {"id": "gamedev", "name": "Game Dev", "sealed": True},
    {"id": "content", "name": "Content", "sealed": False},
]


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def floor_state(*, settings, architect, comms, events, scheduler=None, missions=None, approvals=None, forge=None,
                leads=None, halt=None, finance=None, dayjob=None, observer=None, content=None) -> dict:
    busy = set(events.busy)
    since = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    recent = events.recent(limit=60)
    errored = {e["source"] for e in recent if e["kind"] == "error" and e["ts"] >= since}
    last_by = {}
    for e in recent:
        last_by[e["source"]] = e  # oldest-first list, so the last one wins

    nxt = {}
    if scheduler is not None:
        for j in scheduler.get_jobs():
            key = j.id.split("_")[0]
            t = getattr(j, "next_run_time", None)
            if t and (key not in nxt or t < nxt[key]):
                nxt[key] = t

    pending = approvals.pending() if approvals else []
    llm = architect.llm
    paused = llm.paused.is_set()
    halted = bool(halt and halt.is_set())
    local = llm.ollama.model
    arch_claude = settings.architect_brain_effective == "claude-api"
    brain = f"Claude ({settings.model_think})" if arch_claude else local

    def state(source, sleeper=False):
        if source in errored:
            return "blocked"
        if source in busy:
            return "thinking" if source in ("architect", "clair") else "working"
        if paused and source not in ("clair", "forge") and not (source == "architect" and arch_claude):
            return "asleep"  # The Forge has the GPU
        return "asleep" if sleeper else "idle"

    def last(source):
        e = last_by.get(source)
        return {"text": e["text"], "ts": e["ts"], "kind": e["kind"]} if e else None

    agents = [
        {"id": "architect", "name": "The Architect", "role": "Overseer", "room": "command", "kind": "commander",
         "state": "blocked" if pending and "architect" not in busy else state("architect"),
         "vendor": "anthropic" if arch_claude else "local", "model": brain,
         "next": _iso(nxt.get("commander")), "last": last("architect"),
         "waiting_on_you": len(pending)},
        {"id": "clair", "name": "C.L.a.i.R", "role": "Comms", "room": "comms", "kind": "assistant",
         "state": state("clair"), "vendor": "google" if comms.clair_mode == "cloudflare" else "local",
         "model": "Gemini" if comms.clair_mode == "cloudflare" else local, "last": last("clair"),
         "link": comms.clair_mode},
        {"id": "intel", "name": "Intel", "role": "Sleeper", "room": "intel", "kind": "sleeper",
         "state": state("intel", sleeper=True), "vendor": "local", "model": local, "next": _iso(nxt.get("intel")),
         "last": last("intel")},
        {"id": "revenue", "name": "Revenue", "role": "Sleeper", "room": "revenue", "kind": "sleeper",
         "state": state("revenue", sleeper=True), "vendor": "local", "model": local, "next": _iso(nxt.get("revenue")),
         "last": last("revenue"), "pipeline": leads.counts() if leads else {},
         "followups_due": leads.due_count() if leads else 0},
        {"id": "learner", "name": "The Archivist", "role": "Sleeper", "room": "archives", "kind": "sleeper",
         "state": state("learner", sleeper=True), "vendor": "local", "model": local, "next": _iso(nxt.get("learner")),
         "last": last("learner")},
    ]
    if finance is not None:
        agents.append({"id": "finance", "name": "The Bookkeeper", "role": "Sleeper", "room": "finance", "kind": "sleeper",
                       "state": state("finance", sleeper=True), "vendor": "local", "model": local,
                       "next": _iso(nxt.get("finance")), "last": last("finance"),
                       "money": {"due_this_week": len(finance.upcoming(7)), "uncategorized": len(finance.uncategorized_merchants(500)),
                                 "transactions": finance.count()}})

    if dayjob is not None:
        c = dayjob.counts()
        agents.append({"id": "dayjob", "name": "The Technician", "role": "Sleeper", "room": "dayjob", "kind": "sleeper",
                       "state": state("dayjob", sleeper=True), "vendor": "local", "model": local,
                       "next": _iso(nxt.get("dayjob")), "last": last("dayjob"),
                       "support": {"open": c.get("open", 0) + c.get("waiting", 0), "fixes": c.get("fixes", 0), "docs": c.get("docs", 0)}})

    if observer is not None:
        devs = observer.devices()
        watching = any(d["live"] for d in devs) and not observer.paused()
        agents.append({"id": "observer", "name": "The Observer", "role": "Learns your day", "room": "archives", "kind": "sleeper",
                       "state": "blocked" if "observer" in errored else ("working" if watching else "idle"),
                       "vendor": "local", "model": local, "last": last("observer"),
                       "watch": {"devices": [d["name"] for d in devs if d["live"]], "paused": observer.paused()}})

    if content is not None:
        c = content.counts()
        agents.append({"id": "content", "name": "The Writer", "role": "Sleeper", "room": "content", "kind": "sleeper",
                       "state": state("content", sleeper=True), "vendor": "local", "model": local,
                       "next": _iso(nxt.get("content")), "last": last("content"),
                       "writing": {"ideas": c.get("idea", 0), "drafts": c.get("draft", 0), "scheduled": c.get("scheduled", 0),
                                   "posted": c.get("posted", 0)}})

    forge_info = {}
    if forge is not None:
        h = forge.health()
        jobs = forge.jobs(3)
        forge_info = {"online": bool(h.get("ok")), "gpu": h.get("gpu", ""), "util": h.get("gpu_util"),
                      "temp": h.get("gpu_temp"), "mem_used": h.get("gpu_mem_used"), "mem_total": h.get("gpu_mem_total"),
                      "training": paused, "job": next((j for j in jobs if j["status"] in ("running", "importing")), None),
                      "queued": [j["id"] for j in jobs if j["status"] == "queued"], "lessons": forge.lessons(),
                      "drills": comms.drills.stats() if getattr(comms, "drills", None) else None}
        agents.append({"id": "forge", "name": "Forge Operator", "role": "The Forge", "room": "forge", "kind": "forge",
                       "state": "blocked" if "forge" in errored else ("working" if paused or "forge" in busy else "idle"),
                       "vendor": "local", "model": settings.forge_base_model.split("/")[-1], "last": last("forge")})

    active = []
    if missions is not None:
        for m in missions.recent(30):
            if m["status"] in ("running", "queued"):
                active.append({"id": m["id"], "title": m["title"], "status": m["status"], "kind": m.get("kind", "research"),
                               "source": m["source"]})
        for i, m in enumerate(active[:6]):
            agents.append({"id": f"splinter-{m['id']}", "name": f"Splinter {m['id']:03d}", "role": m["title"],
                           "room": "codebay" if m["kind"] == "code" else "ready", "kind": "splinter",
                           "state": "working" if m["status"] == "running" and not halted else "idle",
                           "vendor": "local", "model": local, "mission": m["id"]})

    if halted:
        for a in agents:
            if a["state"] in ("working", "thinking") and a["id"] not in ("clair",):
                a["state"] = "idle"

    mtd = architect.ledger.month_to_date()
    from .health import snapshot

    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "rooms": ROOMS,
        "agents": agents,
        "hud": {"spend": round(mtd, 2), "budget": settings.monthly_budget_usd, "host": snapshot(settings.data_dir),
                "gpu": forge_info, "approvals": [{"id": a["id"], "summary": a["summary"], "agent": a["agent"],
                                                   "reason": a["reason"]} for a in pending],
                "missions": active, "halted": halted, "clair_link": comms.clair_mode},
        "events": recent[-14:],
    }
