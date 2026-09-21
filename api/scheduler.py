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

# daily.py exit codes: 0 appended, 1 nothing new, 2 fetch or write error
OK = {0, 1}

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


def main():
    log.info("scheduler up; catching up once before the first scheduled run")
    for _, _, mode in SCHEDULE:
        run(mode)

    while True:
        when, mode = next_run(datetime.now())
        wait = (when - datetime.now()).total_seconds()
        log.info("next: %s at %s (in %.1fh)", mode, when.strftime("%Y-%m-%d %H:%M"),
                 wait / 3600)
        time.sleep(max(wait, 0))
        run(mode)


if __name__ == "__main__":
    main()
