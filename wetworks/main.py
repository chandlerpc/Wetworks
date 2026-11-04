"""Entry point.

    python -m wetworks.main                 run everything: scheduler + Command API/console
    python -m wetworks.main brief morning   send one brief now and exit
    python -m wetworks.main run intel       run one cell now and print what it found
    python -m wetworks.main mission <text>  run one research mission now and print the result
    python -m wetworks.main code <text>     run one code mission now (opens a pull request)
    python -m wetworks.main cycle           run one Architect command cycle now and print his report
    python -m wetworks.main import-chats <conversations.json or export .zip>   load past Claude chats
"""

import logging
import re
import sys

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .architect import Architect
from .bridge import ClairBridge
from .cells.base import CellContext
from .cells.intel import Intel
from .cells.learner import Learner
from .agent.brains import clair_ask, make_ask
from .agent.clair import ClairAgent
from .agent.commander import Commander
from .agent.state import Approvals, Followups
from .agent.toolkit import build_toolbox
from .agent.traces import Traces, recorded
from .forge import Forge
from .halt import Halt
from .knowledge import ChatArchive, sync_knowledge
from .memory import Memory
from .cells.revenue import Revenue
from .leads import LeadStore
from .cells.coder import Coder
from .cells.splinter import Splinter
from .code.brain import make_brain
from .code.workspace import Workspace
from .comms import Comms
from .config import load_settings
from .missions import MissionStore
from .operators import SleeperOperator, SplinterOperator
from .voice import Voice

log = logging.getLogger("wetworks")


def _cron(hhmm: str, tz: str) -> CronTrigger:
    h, m = hhmm.split(":")
    return CronTrigger(hour=int(h), minute=int(m), timezone=tz)


def build(settings):
    architect = Architect(settings)
    memory = Memory(settings.data_dir / "memory.db")
    loaded = memory.sync_profile(settings.config_dir / "profile.md")
    known = sync_knowledge(memory, settings.config_dir / "knowledge")
    chats = ChatArchive(settings.data_dir / "chats.db")
    log.info("Memory: %d profile lines, %d knowledge lines, %d memories total, %d archived chats",
             loaded, known, memory.count(), chats.count())
    # Every agent decision is recorded for The Forge; brain says who made it (teachers: gemini, claude).
    traces = Traces(settings.data_dir / "traces.db")
    halt = Halt(settings.data_dir / "HALTED")
    default_brain = lambda: "claude" if settings.agent_brain == "claude-api" else "local"
    forge = Forge(settings=settings, llm=architect.llm, traces=traces, events=architect.events,
                  data_dir=settings.data_dir / "forge", runner_url=settings.forge_url, ollama_url=settings.ollama_url)
    active = settings.data_dir / "forge" / "active_model.txt"
    if active.exists() and active.read_text().strip():
        architect.llm.ollama.model = active.read_text().strip()  # a model promoted from The Forge
    ctx = CellContext(settings=settings, llm=architect.llm, findings=architect.findings, events=architect.events,
                      memory=memory, ask=recorded(make_ask(settings, architect.llm, "cells"), traces, "cells", default_brain),
                      halt=halt)
    forge.halt = halt
    intel = Intel(ctx)
    leads = LeadStore(settings.data_dir / "leads.db")
    revenue = Revenue(ctx, leads)
    architect.leads = leads
    from .cells.finance import FinanceCell
    from .finance import Finance

    finance = Finance(settings.data_dir / "finance.db", settings.config_dir / "finance.toml")
    finance_cell = FinanceCell(ctx, finance)
    from .cells.dayjob import DayJobCell
    from .dayjob import DayJob

    dayjob = DayJob(settings.data_dir / "dayjob.db")
    dayjob_cell = DayJobCell(ctx, dayjob)
    from .cells.observer import ObserverCell
    from .observer import Observer

    observer = Observer(settings.data_dir / "observer.db", settings.config_dir / "observer.toml", tz=settings.timezone)
    observer_cell = ObserverCell(ctx, observer)
    from .cells.content import ContentCell
    from .content import Content

    content = Content(settings.data_dir / "content.db", settings.config_dir / "content.toml")
    architect.min_importance = int(intel.config.get("intel", {}).get("min_importance_for_brief", 2))
    missions = MissionStore(settings.data_dir / "missions.db")
    coder = Coder(ctx, missions,
                  Workspace(settings.data_dir / "workspaces", settings.code_repo, token=settings.github_token),
                  make_brain(settings.code_brain, architect.llm, settings.code_max_files),
                  runs_dir=settings.data_dir / "sandbox-runs", sandbox_url=settings.sandbox_url,
                  repair_rounds=settings.code_repair_rounds)
    operator = SplinterOperator(missions, Splinter(ctx, missions), events=architect.events, notify=architect.notify,
                                runners={"code": coder})

    def open_mission(title: str, request: str, source: str, kind: str = "research") -> dict:
        m = missions.create(title, request, source=source, kind=kind)
        architect.events.add("splinter", "info", f"{'Code m' if m['kind'] == 'code' else 'M'}ission #{m['id']} queued: {m['title']}")
        operator.submit()
        return m

    bridge = ClairBridge(settings.clair_url, settings.clair_token, owner=settings.owner_name)
    comms = Comms(settings.data_dir / "comms.db", architect.llm, architect.events, architect.context_block,
                  open_mission=open_mission, bridge=bridge, memory=memory)
    operator.on_done = comms.mission_report
    comms.missions, comms.operator, comms.open_mission_fn = missions, operator, open_mission
    revenue.open_mission = lambda title, request, source: open_mission(title, request, source)
    comms.leads = leads
    comms.finance = finance
    comms.dayjob = dayjob
    comms.observer = observer
    comms.content = content
    content_cell = ContentCell(ctx, content, missions=missions, observer=observer)
    learner = Learner(ctx, comms, missions)

    # ---- agents ----
    def notify_approval(a: dict) -> None:
        architect.notify.send(f"🟠 **Needs your approval** #{a['id']}: {a['summary']}\nWhy: {a['reason'] or '(not given)'}"
                              "\nApprove or decline in the console (Ops → Needs you).")
        comms._insert("architect", "architect", f"I need your OK on #{a['id']}: {a['summary']}. It's under Ops → Needs you.")

    approvals = Approvals(settings.data_dir / "agents.db", on_request=notify_approval)
    observer_cell.approvals = approvals

    def adopt_observation(kind, text="", which="", time="", title="", request=""):
        """Runs when Patrick approves one of The Observer's suggestions."""
        if kind == "preference":
            memory.add(str(text), kind="preference", source="observer")
            return "Saved as a standing preference."
        if kind == "mission":
            m = open_mission(str(title), str(request), "observer")
            return f"Opened mission #{m['id']}: {m['title']}"
        if kind == "brief_time" and which in ("morning", "evening") and re.fullmatch(r"\d{2}:\d{2}", str(time)):
            save_override(settings, f"{which}_brief", str(time))
            return (f"The {which} brief will move to {time}. It takes effect the next time Wetworks restarts "
                    "(docker compose restart architect).")
        return "Nothing to do for that suggestion."

    approvals.executors["adopt_observation"] = adopt_observation
    followups = Followups(settings.data_dir / "agents.db")

    agent_ask = recorded(make_ask(settings, architect.llm, "architect"), traces, "architect", default_brain)

    toolbox = build_toolbox(llm=architect.llm, memory=memory, missions=missions, open_mission=open_mission,
                            findings=architect.findings, leads=leads, revenue=revenue, notify=architect.notify,
                            followups=followups, workspace=coder.ws, daily_mission_cap=settings.agent_daily_missions,
                            chats=chats, forge=forge, finance=finance,
                            finance_level=finance_level(settings, finance, cloud=settings.agent_brain != "local"),
                            dayjob=dayjob, dayjob_local=settings.agent_brain == "local",
                            observer=observer, observer_local=settings.agent_brain == "local", content=content)
    for name in ("merge_pull_request", "switch_local_model"):
        if name in toolbox.tools:
            approvals.executors[name] = toolbox.tools[name].fn
    commander = Commander(toolbox=toolbox, ask=agent_ask, approvals=approvals, followups=followups,
                          events=architect.events, context_fn=architect.context_block, memory=memory,
                          max_steps=settings.agent_max_steps, paused=architect.llm.paused)
    comms.architect_agent = commander.chat

    # C.L.a.i.R: her own toolbox (missions she opens report back to her), Gemini brain with local backup.
    def clair_mode(mode: str, why: str) -> None:
        if mode != comms.clair_mode:
            msg = {"cloudflare": "Thinking on Cloudflare (Gemini) again",
                   "fallback": f"Cloudflare unavailable ({why}); thinking on the local model",
                   "local": "Thinking on the local model (no Cloudflare bridge configured)"}[mode]
            architect.events.add("clair", "warn" if mode == "fallback" else "info", msg)
        comms.clair_mode = mode

    clair_tools = build_toolbox(llm=architect.llm, memory=memory, missions=missions, open_mission=open_mission,
                                findings=architect.findings, leads=leads, revenue=revenue, notify=architect.notify,
                                followups=followups, agent_name="clair", daily_mission_cap=settings.agent_daily_missions,
                                commander=commander, chats=chats, finance=finance,
                                finance_level=finance_level(settings, finance, cloud=True),
                                dayjob=dayjob, dayjob_local=False, observer=observer, observer_local=False, content=content)
    clair_brain = lambda: "gemini" if comms.clair_mode == "cloudflare" else default_brain()
    clair = ClairAgent(toolbox=clair_tools, ask=recorded(clair_ask(bridge, make_ask(settings, architect.llm, "clair"), clair_mode),
                                                         traces, "clair", clair_brain),
                       approvals=approvals, events=architect.events, context_fn=architect.context_block, memory=memory)
    comms.clair_agent = clair.chat
    previous_done = operator.on_done

    def on_done(m: dict) -> None:
        if revenue.mission_finished(m):  # lead research: the Revenue cell files it and drafts outreach
            return
        previous_done(m)
        commander.on_mission_done(m)  # The Architect reviews finished work and decides next steps

    operator.on_done = on_done
    comms.commander, comms.approvals, comms.chats, comms.forge = commander, approvals, chats, forge
    from .agent.drills import Drills

    comms.drills = Drills(path=settings.data_dir / "traces.db", traces=traces, bridge=bridge, clair_tools=clair_tools,
                          architect_tools=toolbox, context_fn=architect.context_block, events=architect.events,
                          leads=leads, missions=missions, settings=settings, lessons_fn=forge.lessons)
    commander.halt = operator.halt = comms.halt = comms.drills.halt = halt
    return architect, {"intel": intel, "revenue": revenue, "learner": learner, "finance": finance_cell, "dayjob": dayjob_cell, "observer": observer_cell, "content": content_cell}, comms


def load_overrides(settings) -> None:
    """Settings changed from the console (e.g. an approved Observer suggestion) win over .env."""
    import json as _json

    p = settings.data_dir / "overrides.json"
    try:
        for k, v in _json.loads(p.read_text()).items():
            if k in ("morning_brief", "evening_brief"):
                setattr(settings, k, v)
    except (OSError, ValueError):
        pass


def save_override(settings, key: str, value) -> None:
    import json as _json

    p = settings.data_dir / "overrides.json"
    try:
        cur = _json.loads(p.read_text())
    except (OSError, ValueError):
        cur = {}
    cur[key] = value
    p.write_text(_json.dumps(cur, indent=2))


def finance_level(settings, finance, cloud: bool) -> str:
    """How much of Patrick's money an agent may see: local brains get detail; cloud brains only what
    config/finance.toml allows (nothing unless share_with_cloud = true)."""
    cfg = finance.config()
    if not cloud:
        return "local"
    if not cfg.get("share_with_cloud", False):
        return "none"
    return "counts" if cfg.get("brief") == "counts" else "totals"


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    load_overrides(settings)
    architect, cells, comms = build(settings)

    if len(sys.argv) >= 3 and sys.argv[1] == "brief":
        architect.brief(sys.argv[2])
        return
    if len(sys.argv) >= 3 and sys.argv[1] in ("mission", "code"):
        kind = "code" if sys.argv[1] == "code" else "research"
        m = comms.open_mission_fn("", " ".join(sys.argv[2:]), "console", kind)
        done = comms.operator.run_one(comms.missions.get(m["id"]))
        print(f"Mission #{done['id']} {done['status']}: {done['title']}\n\n{done['result']}")
        for s in done["sources"]:
            print(f"[{s['n']}] {s['title']} {s['url']}")
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "import-chats":
        from pathlib import Path

        n, skipped = comms.chats.import_claude_export(Path(sys.argv[2]))
        print(f"Imported {n} chats ({skipped} empty ones skipped). {comms.chats.count()} in the archive.")
        return
    if len(sys.argv) >= 2 and sys.argv[1].startswith("forge"):
        import json as _json

        forge, cmd, rest = comms.forge, sys.argv[1], sys.argv[2:]
        if cmd == "forge-status":
            print(_json.dumps({"forge": forge.health(), "lessons": forge.lessons(), "drills": comms.drills.stats(),
                               "datasets": forge.datasets(), "jobs": forge.jobs(5),
                               "decisions": forge.traces.counts(), "models": forge.models(), "evals": forge.evals(5)}, indent=2))
        elif cmd == "forge-dataset":
            print(forge.build_agent_dataset())
        elif cmd == "forge-train" and rest:
            j = forge.queue(rest[0], rest[1] if len(rest) > 1 and not rest[1].startswith("--") else "agent-steps",
                            run_now="--now" in rest)
            print(f"Queued {j['id']}" + (" (starts within 5 minutes)" if j["run_now"] else " for tonight's window"))
        elif cmd == "forge-eval" and rest:
            print(forge.evaluate(rest[0], rest[1] if len(rest) > 1 else "agent-steps"))
        else:
            print("forge-status | forge-dataset | forge-train <name> [dataset] [--now] | forge-eval <model> [dataset]")
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "drill":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        for _ in range(n):
            r = comms.drills.run_one()
            print(f"{'ok' if r['ok'] else 'skipped'}: {r.get('agent', '')} {r.get('request', '')!r} "
                  f"{r.get('steps', 0)} lessons. {r.get('final') or r.get('note', '')}"[:400])
            if not r["ok"]:
                break
        print(comms.forge.lessons())
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "cycle":
        print(comms.commander.cycle("manual"))
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "run":
        for f in cells[sys.argv[2]].safe_run():
            print(f"[{f.topic}] ({f.importance}) {f.title}\n  {f.summary}\n  {f.url}")
        return

    sched = BackgroundScheduler(timezone=settings.timezone)
    for kind, hhmm in (("morning", settings.morning_brief), ("evening", settings.evening_brief)):
        sched.add_job(architect.brief, _cron(hhmm, settings.timezone), args=[kind],
                      id=f"{kind}_brief", misfire_grace_time=3600, coalesce=True)

    sleepers = SleeperOperator(sched, settings.timezone)
    sleepers.register(cells["intel"], [settings.morning_brief, settings.evening_brief],
                      lead_minutes=settings.intel_lead_minutes)
    sleepers.register(cells["revenue"], [settings.morning_brief], lead_minutes=settings.revenue_lead_minutes)
    sleepers.register(cells["finance"], [settings.morning_brief], lead_minutes=20)
    sleepers.register(cells["dayjob"], [settings.morning_brief], lead_minutes=15)
    sleepers.register(cells["content"], [settings.morning_brief], lead_minutes=30)
    sleepers.register(cells["learner"], [settings.learner_time])
    sched.add_job(cells["observer"].safe_run, CronTrigger(hour=23, minute=15, timezone=settings.timezone),
                  id="observer_nightly", misfire_grace_time=3600, coalesce=True)
    mh, mm = map(int, settings.morning_brief.split(":"))
    t = (mh * 60 + mm - 10) % (24 * 60)
    sched.add_job(cells["observer"].morning_report, CronTrigger(hour=t // 60, minute=t % 60, timezone=settings.timezone),
                  id="observer_morning", misfire_grace_time=3600, coalesce=True)
    sched.add_job(comms.forge.tick, "interval", minutes=5, id="forge_tick", max_instances=1, coalesce=True)
    sched.add_job(comms.drills.tick, "interval", minutes=max(5, settings.drill_every_minutes), id="drill_tick",
                  max_instances=1, coalesce=True)
    sched.add_job(comms.commander.cycle, CronTrigger(hour=f"{settings.commander_hours}/{settings.commander_every_hours}",
                                                     minute=10, timezone=settings.timezone),
                  args=["scheduled check"], id="commander_cycle", misfire_grace_time=1800, coalesce=True, max_instances=1)
    sched.start()
    comms.operator.start()

    if comms.bridge:
        ok, why = comms.bridge.check()
        comms.clair_mode = "cloudflare" if ok else "fallback"
        architect.events.add("clair", "info" if ok else "warn",
                             "Connected to C.L.a.i.R on Cloudflare" if ok else f"C.L.a.i.R bridge not working: {why}. Answering locally.")
    else:
        architect.events.add("clair", "info", "C.L.a.i.R is running locally (no Cloudflare bridge configured)")

    intel = cells["intel"]
    trial = " (2-week trial)" if intel.in_trial() else ""
    source = {"local": f"local {settings.local_model} + SearXNG (free)", "grok": "Grok (paid)",
              "claude": "Claude web search (paid)"}.get(settings.provider, settings.provider)
    online = (f"The Architect is online. Intel cell active{trial}: {len(intel.topics)} topics via {source}, "
              f"running {settings.intel_lead_minutes} min before each brief.")
    log.info(online)
    architect.events.add("architect", "info", online)
    architect.notify.send(f"🟢 {online}")

    if not settings.console_token:
        log.warning("WETWORKS_CONSOLE_TOKEN is not set, so the console is off. Scheduler still running.")
        architect.events.add("architect", "warn", "Console is off: set WETWORKS_CONSOLE_TOKEN in .env")
        import threading

        threading.Event().wait()  # keep the scheduler alive

    import uvicorn

    from .api import create_app

    app = create_app(settings=settings, architect=architect, cells=cells, comms=comms,
                     events=architect.events, scheduler=sched, missions=comms.missions,
                     open_mission=comms.open_mission_fn, voice=Voice(settings, architect.ledger,
                                                                 vocab_fn=lambda: [l["business"] for l in comms.leads.recent(limit=12)]), leads=comms.leads,
                     memory=comms.memory, approvals=comms.approvals, commander=comms.commander, forge=comms.forge,
                     halt=comms.halt, finance=comms.finance, dayjob=comms.dayjob, observer=comms.observer, content=comms.content)
    log.info("Console listening on port %d", settings.console_port)
    uvicorn.run(app, host="0.0.0.0", port=settings.console_port, log_level="warning")
    sched.shutdown(wait=False)


if __name__ == "__main__":
    run()
