"""Entry point.

    python -m wetworks.main                 run everything: scheduler + Command API/console
    python -m wetworks.main brief morning   send one brief now and exit
    python -m wetworks.main run intel       run one cell now and print what it found
    python -m wetworks.main mission <text>  run one mission now and print the result
"""

import logging
import sys

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .architect import Architect
from .bridge import ClairBridge
from .cells.base import CellContext
from .cells.intel import Intel
from .cells.splinter import Splinter
from .comms import Comms
from .config import load_settings
from .missions import MissionStore
from .operators import SleeperOperator, SplinterOperator

log = logging.getLogger("wetworks")


def _cron(hhmm: str, tz: str) -> CronTrigger:
    h, m = hhmm.split(":")
    return CronTrigger(hour=int(h), minute=int(m), timezone=tz)


def build(settings):
    architect = Architect(settings)
    ctx = CellContext(settings=settings, llm=architect.llm, findings=architect.findings, events=architect.events)
    intel = Intel(ctx)
    architect.min_importance = int(intel.config.get("intel", {}).get("min_importance_for_brief", 2))
    missions = MissionStore(settings.data_dir / "missions.db")
    operator = SplinterOperator(missions, Splinter(ctx, missions), events=architect.events, notify=architect.notify)

    def open_mission(title: str, request: str, source: str) -> dict:
        m = missions.create(title, request, source=source)
        architect.events.add("splinter", "info", f"Mission #{m['id']} queued: {m['title']}")
        operator.submit()
        return m

    bridge = ClairBridge(settings.clair_url, settings.clair_token, owner=settings.owner_name)
    comms = Comms(settings.data_dir / "comms.db", architect.llm, architect.events, architect.context_block,
                  open_mission=open_mission, bridge=bridge)
    operator.on_done = comms.mission_report
    comms.missions, comms.operator, comms.open_mission_fn = missions, operator, open_mission
    return architect, {"intel": intel}, comms


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    architect, cells, comms = build(settings)

    if len(sys.argv) >= 3 and sys.argv[1] == "brief":
        architect.brief(sys.argv[2])
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "mission":
        m = comms.open_mission_fn("", " ".join(sys.argv[2:]), "console")
        done = comms.operator.run_one(comms.missions.get(m["id"]))
        print(f"Mission #{done['id']} {done['status']}: {done['title']}\n\n{done['result']}")
        for s in done["sources"]:
            print(f"[{s['n']}] {s['title']} {s['url']}")
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
    sched.start()
    comms.operator.start()

    if comms.bridge:
        ok, why = comms.bridge.check()
        comms.clair_mode = "cloudflare" if ok else "fallback"
        architect.events.add("clair", "info" if ok else "warn",
                             "Connected to clAIr on Cloudflare" if ok else f"clAIr bridge not working: {why}. Answering locally.")
    else:
        architect.events.add("clair", "info", "clAIr is running locally (no Cloudflare bridge configured)")

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
                     open_mission=comms.open_mission_fn)
    log.info("Console listening on port %d", settings.console_port)
    uvicorn.run(app, host="0.0.0.0", port=settings.console_port, log_level="warning")
    sched.shutdown(wait=False)


if __name__ == "__main__":
    run()
