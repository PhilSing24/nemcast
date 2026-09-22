"""Pull from the NEMCast API into local ClickHouse — a rehearsal of CaféBot's role.

    python src/consume.py                 # last 2 days, all four tables
    python src/consume.py --days 7        # wider window, to recover a gap
    python src/consume.py --table dispatchprice

For each table it:

    1. requests a rolling window from the API and follows every page
    2. loads the rows into a temporary staging table
    3. inserts only rows whose key is not already stored — a merge, so
       rerunning over an overlapping window never duplicates anything
    4. records the run in nemcast.ingest_batches

The window deliberately overlaps the previous run. A missed run is then
covered by the next one, and the key-based merge absorbs the overlap.

Needs NEMCAST_API_KEY — read from the environment, or from .env.

Exit codes: 0 success, 2 any table failed.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
NEM = ZoneInfo("Australia/Brisbane")
DB = "nemcast"

CLICKHOUSE = [str(Path.home() / "clickhouse"), "client", "--host", "::1"]

# table -> (time column, key columns). The key is what makes a row unique;
# for forecasts it includes the run (vintage), or every run collapses into one.
TABLES = {
    "dispatchprice":        ("SETTLEMENTDATE", ["REGIONID", "SETTLEMENTDATE", "INTERVENTION", "source"]),
    "dispatchregionsum":    ("SETTLEMENTDATE", ["REGIONID", "SETTLEMENTDATE", "INTERVENTION", "source"]),
    "predispatchprice":     ("DATETIME", ["REGIONID", "DATETIME", "vintage", "INTERVENTION", "source"]),
    "predispatchregionsum": ("DATETIME", ["REGIONID", "DATETIME", "vintage", "INTERVENTION", "source"]),
}

PAGE = 50_000
RETRYABLE = {429, 500, 502, 503, 504}


# ------------------------------------------------------------------ config

def api_key():
    key = os.environ.get("NEMCAST_API_KEY")
    if not key and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("NEMCAST_API_KEY="):
                key = line.split("=", 1)[1].strip()
    if not key:
        sys.exit("NEMCAST_API_KEY not set and not found in .env")
    return key


# ------------------------------------------------------------------ clickhouse

def ch(sql, data=None):
    """Run one statement. Raises with ClickHouse's own message on failure."""
    r = subprocess.run(
        CLICKHOUSE + ["--query", sql,
                      "--date_time_input_format", "best_effort",
                      "--input_format_skip_unknown_fields", "1"],
        input=data, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[:1500])
    return r.stdout.strip()


def ensure_batch_log():
    ch(f"""
        CREATE TABLE IF NOT EXISTS {DB}.ingest_batches
        (
            batch_id     String,
            table_name   LowCardinality(String),
            window_start String,
            window_end   String,
            pulled       UInt64,
            inserted     UInt64,
            already_held UInt64,
            status       LowCardinality(String),
            message      String,
            started_at   DateTime,
            finished_at  DateTime
        )
        ENGINE = MergeTree
        ORDER BY (table_name, started_at)
    """)


def log_batch(row):
    ch(f"INSERT INTO {DB}.ingest_batches FORMAT JSONEachRow", json.dumps(row) + "\n")


# ------------------------------------------------------------------ api

def get(url, params, headers, attempts=4):
    """GET with back-off on transient errors; fail at once on the rest.

    A 401 or 400 will not fix itself by retrying, so hammering the API over it
    only hides the real problem.
    """
    delays = [2, 5, 15]
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=120)
        except requests.RequestException as exc:
            if i == attempts - 1:
                raise
            print(f"    network error ({exc.__class__.__name__}), retrying in {delays[i]}s")
            time.sleep(delays[i])
            continue
        if r.status_code in RETRYABLE and i < attempts - 1:
            print(f"    HTTP {r.status_code}, retrying in {delays[i]}s")
            time.sleep(delays[i])
            continue
        r.raise_for_status()
        return r.json()


def pull(base, key, table, days):
    """Every row in the window, following pages until the API says there are no more."""
    rows, offset, window = [], 0, None
    while True:
        page = get(f"{base}/data/{table}",
                   {"days": days, "limit": PAGE, "offset": offset, "format": "json"},
                   {"X-API-Key": key})
        window = window or (page["start"], page["end"])
        rows.extend(page["data"])
        if page["next_offset"] is None:
            return rows, window
        offset = page["next_offset"]


# ------------------------------------------------------------------ merge

def merge(table, rows, window):
    """Stage the pull, then insert only keys not already stored.

    ClickHouse does not deduplicate on insert, so appending an overlapping
    window would double the overlap. Comparing keys first makes the run
    idempotent without ever deleting or rewriting stored rows.
    """
    tcol, keys = TABLES[table]
    stage = f"{DB}._stage_{table}"
    key = ", ".join(keys)
    start, end = window

    ch(f"DROP TABLE IF EXISTS {stage}")
    ch(f"CREATE TABLE {stage} AS {DB}.{table} ENGINE = Memory")
    try:
        if rows:
            ch(f"INSERT INTO {stage} FORMAT JSONEachRow",
               "\n".join(json.dumps(r) for r in rows) + "\n")

        # Time bounds as literals, so ClickHouse reads them in the column's own
        # time zone — the same frame as the stored values.
        held = int(ch(f"""
            SELECT count() FROM {stage}
            WHERE ({key}) IN (
                SELECT {key} FROM {DB}.{table}
                WHERE {tcol} >= '{start} 00:00:00' AND {tcol} < '{end} 00:00:00')
        """) or 0)

        ch(f"""
            INSERT INTO {DB}.{table}
            SELECT * FROM {stage}
            WHERE ({key}) NOT IN (
                SELECT {key} FROM {DB}.{table}
                WHERE {tcol} >= '{start} 00:00:00' AND {tcol} < '{end} 00:00:00')
        """)
        return len(rows) - held, held
    finally:
        ch(f"DROP TABLE IF EXISTS {stage}")


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--table", choices=list(TABLES))
    ap.add_argument("--api", default=os.environ.get("NEMCAST_API_URL", "http://localhost:8000"))
    args = ap.parse_args()

    key = api_key()
    ensure_batch_log()
    batch = datetime.now(NEM).strftime("%Y%m%d%H%M%S-") + uuid.uuid4().hex[:6]
    tables = [args.table] if args.table else list(TABLES)
    failures = 0

    print(f"batch {batch} — last {args.days} days from {args.api}\n")
    for table in tables:
        started = datetime.now(NEM).replace(tzinfo=None)
        record = dict(batch_id=batch, table_name=table, window_start="", window_end="",
                      pulled=0, inserted=0, already_held=0, status="failed", message="",
                      started_at=started.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            rows, window = pull(args.api, key, table, args.days)
            inserted, held = merge(table, rows, window)
            latest = ch(f"SELECT max({TABLES[table][0]}) FROM {DB}.{table}")
            record.update(window_start=window[0], window_end=window[1], pulled=len(rows),
                          inserted=inserted, already_held=held, status="success",
                          message=f"latest {latest}")
            print(f"  {table:22} pulled {len(rows):>7,}  new {inserted:>7,}  "
                  f"already held {held:>7,}  latest {latest}")
        except Exception as exc:
            failures += 1
            record["message"] = str(exc)[:1000]
            print(f"  {table:22} FAILED: {str(exc)[:300]}")
        finally:
            record["finished_at"] = datetime.now(NEM).strftime("%Y-%m-%d %H:%M:%S")
            try:
                log_batch(record)
            except Exception as exc:
                print(f"  could not record batch: {exc}")

    sys.exit(2 if failures else 0)


if __name__ == "__main__":
    main()
