"""Entry point.

    python -m wetworks.main                 run the scheduler (default; what Docker runs)
    python -m wetworks.main brief morning   send one brief now and exit
    python -m wetworks.main run intel       run one cell now and print what it found
"""

import logging
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .architect import Architect
from .cells.base import CellContext
from .cells.intel import Intel
from .config import load_settings
from .operators import SleeperOperator

log = logging.getLogger("wetworks")


def _cron(hhmm: str, tz: str) -> CronTrigger:
    h, m = hhmm.split(":")
    return CronTrigger(hour=int(h), minute=int(m), timezone=tz)


def build(settings):
    architect = Architect(settings)
    ctx = CellContext(settings=settings, llm=architect.llm, findings=architect.findings)
    intel = Intel(ctx)
    architect.min_importance = int(intel.config.get("intel", {}).get("min_importance_for_brief", 2))
    return architect, {"intel": intel}


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    architect, cells = build(settings)

    if len(sys.argv) >= 3 and sys.argv[1] == "brief":
        architect.brief(sys.argv[2])
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "run":
        for f in cells[sys.argv[2]].safe_run():
            print(f"[{f.topic}] ({f.importance}) {f.title}\n  {f.summary}\n  {f.url}")
        return

    sched = BlockingScheduler(timezone=settings.timezone)
    for kind, hhmm in (("morning", settings.morning_brief), ("evening", settings.evening_brief)):
        sched.add_job(architect.brief, _cron(hhmm, settings.timezone), args=[kind],
                      id=f"{kind}_brief", misfire_grace_time=3600, coalesce=True)

    sleepers = SleeperOperator(sched, settings.timezone)
    sleepers.register(cells["intel"], [settings.morning_brief, settings.evening_brief],
                      lead_minutes=settings.intel_lead_minutes)

    signal.signal(signal.SIGTERM, lambda *_: sched.shutdown(wait=False))
    intel = cells["intel"]
    trial = " (2-week trial)" if intel.in_trial() else ""
    log.info("The Architect is online. Briefs at %s and %s %s (dry_run=%s)",
             settings.morning_brief, settings.evening_brief, settings.timezone, settings.dry_run)
    source = {"local": f"local {settings.local_model} + SearXNG (free)", "grok": "Grok (paid)",
              "claude": "Claude web search (paid)"}.get(settings.provider, settings.provider)
    architect.notify.send(
        f"🟢 The Architect is online. Intel cell active{trial}: {len(intel.topics)} topics via {source}, "
        f"running {settings.intel_lead_minutes} min before each brief."
    )
    sched.start()


if __name__ == "__main__":
    run()
