"""Entry point.

    python -m wetworks.main                 run everything: scheduler + Command API/console
    python -m wetworks.main brief morning   send one brief now and exit
    python -m wetworks.main run intel       run one cell now and print what it found
    python -m wetworks.main mission <text>  run one research mission now and print the result
    python -m wetworks.main code <text>     run one code mission now (opens a pull request)
    python -m wetworks.main cycle           run one Architect command cycle now and print his report
"""

import logging
import sys

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .architect import Architect
from .bridge import ClairBridge
from .cells.base import CellContext
from .cells.intel import Intel
from .cells.learner import Learner
from .agent.commander import Commander
from .agent.state import Approvals, Followups
from .agent.toolkit import build_toolbox
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
    log.info("Memory: %d profile lines, %d memories total", loaded, memory.count())
    ctx = CellContext(settings=settings, llm=architect.llm, findings=architect.findings, events=architect.events,
                      memory=memory)
    intel = Intel(ctx)
    leads = LeadStore(settings.data_dir / "leads.db")
    revenue = Revenue(ctx, leads)
    architect.leads = leads
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
    comms.leads = leads
    learner = Learner(ctx, comms, missions)

    # ---- agents ----
    def notify_approval(a: dict) -> None:
        architect.notify.send(f"🟠 **Needs your approval** #{a['id']}: {a['summary']}\nWhy: {a['reason'] or '(not given)'}"
                              "\nApprove or decline in the console (Ops → Needs you).")
        comms._insert("architect", "architect", f"I need your OK on #{a['id']}: {a['summary']}. It's under Ops → Needs you.")

    approvals = Approvals(settings.data_dir / "agents.db", on_request=notify_approval)
    followups = Followups(settings.data_dir / "agents.db")

    def agent_ask(system: str, prompt: str, max_tokens: int) -> str:
        if settings.agent_brain == "claude-api":
            return architect.llm.ask("agent", prompt, tier="think", system=system, max_tokens=max_tokens).text
        return architect.llm.ask_local("agent", prompt, system=system, json_mode=True, max_tokens=max_tokens).text

    toolbox = build_toolbox(llm=architect.llm, memory=memory, missions=missions, open_mission=open_mission,
                            findings=architect.findings, leads=leads, revenue=revenue, notify=architect.notify,
                            followups=followups, workspace=coder.ws, daily_mission_cap=settings.agent_daily_missions)
    for name in ("merge_pull_request",):
        if name in toolbox.tools:
            approvals.executors[name] = toolbox.tools[name].fn
    commander = Commander(toolbox=toolbox, ask=agent_ask, approvals=approvals, followups=followups,
                          events=architect.events, context_fn=architect.context_block, memory=memory,
                          max_steps=settings.agent_max_steps)
    comms.architect_agent = commander.chat
    previous_done = operator.on_done

    def on_done(m: dict) -> None:
        previous_done(m)
        commander.on_mission_done(m)  # The Architect reviews finished work and decides next steps

    operator.on_done = on_done
    comms.commander, comms.approvals = commander, approvals
    return architect, {"intel": intel, "revenue": revenue, "learner": learner}, comms


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
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
    sleepers.register(cells["learner"], [settings.learner_time])
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
                     open_mission=comms.open_mission_fn, voice=Voice(settings, architect.ledger), leads=comms.leads,
                     memory=comms.memory, approvals=comms.approvals, commander=comms.commander)
    log.info("Console listening on port %d", settings.console_port)
    uvicorn.run(app, host="0.0.0.0", port=settings.console_port, log_level="warning")
    sched.shutdown(wait=False)


if __name__ == "__main__":
    run()
