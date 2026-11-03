"""Entry point.

    python -m wetworks.main            run the scheduler (default; what Docker runs)
    python -m wetworks.main brief morning   send one brief now and exit
"""

import logging
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .architect import Architect
from .config import load_settings

log = logging.getLogger("wetworks")


def _cron(hhmm: str, tz: str) -> CronTrigger:
    h, m = hhmm.split(":")
    return CronTrigger(hour=int(h), minute=int(m), timezone=tz)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    architect = Architect(settings)

    if len(sys.argv) >= 3 and sys.argv[1] == "brief":
        architect.brief(sys.argv[2])
        return

    sched = BlockingScheduler(timezone=settings.timezone)
    sched.add_job(architect.brief, _cron(settings.morning_brief, settings.timezone), args=["morning"],
                  id="morning_brief", misfire_grace_time=3600, coalesce=True)
    sched.add_job(architect.brief, _cron(settings.evening_brief, settings.timezone), args=["evening"],
                  id="evening_brief", misfire_grace_time=3600, coalesce=True)

    signal.signal(signal.SIGTERM, lambda *_: sched.shutdown(wait=False))
    log.info("The Architect is online. Briefs at %s and %s %s (dry_run=%s)",
             settings.morning_brief, settings.evening_brief, settings.timezone, settings.dry_run)
    architect.notify.send("🟢 The Architect is online.")
    sched.start()


if __name__ == "__main__":
    run()
