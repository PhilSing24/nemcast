"""Runs the daily ingestion on schedule, inside the container.

    python api/scheduler.py

Two runs a day, in NEM time (the container sets TZ=Australia/Brisbane, which is
AEST with no daylight saving — the same clock AEMO uses):

    04:30  dispatch     yesterday's actual outcomes
    20:30  predispatch  the forecast covering tomorrow

Kept deliberately simple: a loop that sleeps until the next run. No cron daemon,
no extra dependency, and the logs go straight to the container output.

On start-up it runs both modes once, so a freshly deployed container catches up
immediately instead of waiting up to 24 hours.
"""

import logging
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAILY = ROOT / "src" / "daily.py"

SCHEDULE = [
    (4, 30, "dispatch"),
    (20, 30, "predispatch"),
]

# daily.py exits 0 on success, 2 on fetch errors. Python also exits 1 on an
# unhandled exception, so treating 1 as fine would log crashes as quiet days.
OK = {0}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("scheduler")


def run(mode: str) -> int:
    log.info("starting %s", mode)
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(DAILY), "--mode", mode],
        cwd=ROOT, capture_output=True, text=True)
    elapsed = time.monotonic() - started

    for line in result.stdout.strip().splitlines():
        log.info("  %s", line)
    for line in result.stderr.strip().splitlines():
        log.warning("  %s", line)

    level = logging.INFO if result.returncode in OK else logging.ERROR
    log.log(level, "%s finished in %.0fs, exit %d", mode, elapsed, result.returncode)
    return result.returncode


def next_run(now: datetime):
    """The earliest scheduled (time, mode) strictly after now."""
    candidates = []
    for hour, minute, mode in SCHEDULE:
        t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        candidates.append((t, mode))
    return min(candidates)


POLL_SECONDS = 60
LATE_MINUTES = 5


def main():
    log.info("scheduler up; catching up once before the first scheduled run")
    for _, _, mode in SCHEDULE:
        run(mode)

    when, mode = next_run(datetime.now())
    log.info("next: %s at %s", mode, when.strftime("%Y-%m-%d %H:%M"))

    # Wake every minute and compare against the wall clock, rather than sleeping
    # until the next run in one block. A single long sleep counts elapsed time
    # on a clock that stops while the machine is suspended or the VM is paused:
    # after a night with the lid closed, it was still waiting at 07:00 for a
    # 04:30 run.
    while True:
        time.sleep(POLL_SECONDS)
        now = datetime.now()
        if now < when:
            continue

        if now - when > timedelta(minutes=LATE_MINUTES):
            # Woke up late — possibly past more than one scheduled run. Run
            # every mode, as on start-up, so neither the actuals nor the
            # forecast is left waiting a full day for its next slot.
            log.warning("woke %.0f min late for %s; catching up all modes",
                        (now - when).total_seconds() / 60, mode)
            for _, _, m in SCHEDULE:
                run(m)
        else:
            run(mode)

        when, mode = next_run(datetime.now())
        log.info("next: %s at %s", mode, when.strftime("%Y-%m-%d %H:%M"))


if __name__ == "__main__":
    main()
