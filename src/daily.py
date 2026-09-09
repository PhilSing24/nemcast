"""Daily incremental ingestion from NEMWEB into the warehouse.

Two scheduled runs, one script:

    python src/daily.py --mode predispatch    # 20:30 — tomorrow's forecast
    python src/daily.py --mode dispatch       # 04:30 — yesterday's outcome

Both append to the parquet tables written by backfill.py, using the same schema and
the same dedup key, so re-running is harmless. Files already ingested are skipped by
checking source_file against what is already stored.

    --lookback N   widen the window to recover after a failed run (default 2 days)
    --dry-run      fetch and report without writing

Exit codes: 0 success, 1 nothing fetched, 2 fetch or write error.
"""

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import nemweb as nw
from backfill import (KEEP, REGIONS, REPORT_ALIAS, VINTAGE_KEYS, WAREHOUSE,
                      add_vintage, stamp, tidy)

MODES = {
    "dispatch": {
        "tables": ["DISPATCHPRICE", "DISPATCHREGIONSUM"],
        "paths": ["Reports/Current/DispatchIS_Reports",
                  "Reports/Archive/DispatchIS_Reports"],
        "time_col": "SETTLEMENTDATE",
        # 288 intervals x 5 regions, pricing run only
        "expect_per_day": 1440,
    },
    "predispatch": {
        "tables": ["PREDISPATCHPRICE", "PREDISPATCHREGIONSUM"],
        "paths": ["Reports/Current/PredispatchIS_Reports",
                  "Reports/Archive/PredispatchIS_Reports"],
        "time_col": "DATETIME",
        # ~48 runs x ~55 intervals x 5 regions
        "expect_per_day": 13000,
    },
}


def existing(table):
    """Load the stored table, or an empty frame if it does not exist yet."""
    path = WAREHOUSE / f"{table.lower()}.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def already_have(table):
    """Filenames already ingested, so we do not refetch them."""
    df = existing(table)
    if df.empty or "source_file" not in df.columns:
        return set()
    return set(df.source_file.dropna().unique())


def fetch_window(cfg, start, end, seen):
    """Fetch every report file in the window that is not already stored."""
    frames = {t: [] for t in cfg["tables"]}
    inverse = {v: k for k, v in REPORT_ALIAS.items()}
    request = {inverse.get(t, t) for t in cfg["tables"]}
    fetched = skipped = failed = 0

    for path in cfg["paths"]:
        try:
            names = nw.list_dir(path)
        except Exception as exc:
            print(f"  {path}: {exc}")
            failed += 1
            continue

        tier = "report_current" if "Current" in path else "report_archive"
        picked = []
        for n in names:
            if n in seen:
                skipped += 1
                continue
            ts = nw.file_datetime(n)
            if ts is None:
                m = re.search(r"_(\d{8})", n)
                ts = datetime.strptime(m.group(1), "%Y%m%d") if m else None
            if ts and start <= pd.Timestamp(ts) < end:
                picked.append((n, ts))

        if not picked:
            print(f"  {path}: nothing new")
            continue

        print(f"  {path}: {len(picked)} new files")
        for name, ts in sorted(picked, key=lambda x: x[1]):
            try:
                blocks = nw.fetch_report(path, name, request)
            except Exception as exc:
                print(f"    FAIL {name}: {exc}")
                failed += 1
                continue
            fetched += 1
            for table, df in blocks.items():
                table = REPORT_ALIAS.get(table, table)
                if table not in frames:
                    continue
                df = tidy(df, table)
                if len(df):
                    frames[table].append(
                        stamp(df, tier, pd.Timestamp(ts), name, "file_timestamp"))

    print(f"  fetched {fetched}, skipped {skipped} already stored, {failed} failed")
    return ({t: pd.concat(v, ignore_index=True) for t, v in frames.items() if v},
            fetched, failed)


def append(table, new, dry_run=False):
    """Append to the stored table, deduplicating on the shared key."""
    old = existing(table)
    combined = add_vintage(pd.concat([old, new], ignore_index=True)
                           if len(old) else new)

    key = ["REGIONID"]
    key += ["SETTLEMENTDATE"] if "SETTLEMENTDATE" in combined.columns else ["DATETIME"]
    if "vintage" in combined.columns:
        key.append("vintage")
    if "INTERVENTION" in combined.columns:
        key.append("INTERVENTION")
    key.append("source")

    before = len(combined)
    combined = combined.drop_duplicates(key).sort_values(key).reset_index(drop=True)
    added = len(combined) - len(old)

    tcol = "SETTLEMENTDATE" if "SETTLEMENTDATE" in combined.columns else "DATETIME"
    print(f"  {table}: +{added:,} rows "
          f"({before - len(combined):,} dupes) "
          f"-> {len(combined):,} total, through {combined[tcol].max():%Y-%m-%d %H:%M}")

    if not dry_run:
        WAREHOUSE.mkdir(parents=True, exist_ok=True)
        combined.to_parquet(WAREHOUSE / f"{table.lower()}.parquet", index=False)

    return added


def coverage_check(table, cfg, days=3):
    """Warn if recent days look thin — a job that fetches nothing looks like success."""
    df = existing(table)
    if df.empty:
        return
    tcol = cfg["time_col"]
    recent = df[df[tcol] >= pd.Timestamp.now().normalize() - pd.Timedelta(days=days)]
    if recent.empty:
        print(f"  WARNING {table}: no rows in the last {days} days")
        return
    counts = recent.groupby(recent[tcol].dt.date).size()
    thin = counts[counts < cfg["expect_per_day"] * 0.8]
    # The final day is always partial, so ignore it.
    thin = thin[thin.index < pd.Timestamp.now().date()]
    if len(thin):
        print(f"  WARNING {table}: thin days {dict(thin)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=list(MODES), required=True)
    ap.add_argument("--lookback", type=int, default=2,
                    help="days back to scan; widen to recover a missed run")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = MODES[args.mode]
    now = pd.Timestamp.now()
    start = now.normalize() - pd.Timedelta(days=args.lookback)
    end = now.normalize() + pd.Timedelta(days=2)      # pre-dispatch reaches ahead

    print(f"{now:%Y-%m-%d %H:%M} — {args.mode}")
    print(f"window {start:%Y-%m-%d} -> {end:%Y-%m-%d}"
          f"{' (dry run)' if args.dry_run else ''}\n")

    seen = set()
    for table in cfg["tables"]:
        seen |= already_have(table)

    frames, fetched, failed = fetch_window(cfg, start, end, seen)

    if not frames:
        print("\nnothing new to append")
        # Only an error if we also failed to reach the source.
        sys.exit(2 if failed else 0)

    print()
    total = 0
    for table in cfg["tables"]:
        if table in frames:
            total += append(table, frames[table], args.dry_run)

    print()
    for table in cfg["tables"]:
        coverage_check(table, cfg)

    if failed:
        print(f"\n{failed} fetch failures — rerun with a wider --lookback")
        sys.exit(2)

    sys.exit(0 if total else 1)


if __name__ == "__main__":
    main()
