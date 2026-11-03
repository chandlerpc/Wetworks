"""Entry point.

    python -m wetworks.main                 run everything: scheduler + Command API/console
    python -m wetworks.main brief morning   send one brief now and exit
    python -m wetworks.main run intel       run one cell now and print what it found
"""

import logging
import sys

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .architect import Architect
from .cells.base import CellContext
from .cells.intel import Intel
from .comms import Comms
from .config import load_settings
from .operators import SleeperOperator

log = logging.getLogger("wetworks")


def _cron(hhmm: str, tz: str) -> CronTrigger:
    h, m = hhmm.split(":")
    return CronTrigger(hour=int(h), minute=int(m), timezone=tz)


def build(settings):
    architect = Architect(settings)
    ctx = CellContext(settings=settings, llm=architect.llm, findings=architect.findings, events=architect.events)
    intel = Intel(ctx)
    architect.min_importance = int(intel.config.get("intel", {}).get("min_importance_for_brief", 2))
    comms = Comms(settings.data_dir / "comms.db", architect.llm, architect.events, architect.context_block)
    return architect, {"intel": intel}, comms


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    architect, cells, comms = build(settings)

    if len(sys.argv) >= 3 and sys.argv[1] == "brief":
        architect.brief(sys.argv[2])
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
                     events=architect.events, scheduler=sched)
    log.info("Console listening on port %d", settings.console_port)
    uvicorn.run(app, host="0.0.0.0", port=settings.console_port, log_level="warning")
    sched.shutdown(wait=False)


if __name__ == "__main__":
    run()
