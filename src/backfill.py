"""Build the NEMCast raw layer: MMSDM archives plus the recent report gap.

AEMO publishes MMSDM monthly, roughly a week after month end, so between one and five
weeks of recent data exists only as report files. This covers both, writing to one
schema so the daily job appends to the same tables.

    python src/backfill.py                    # everything from START to today
    python src/backfill.py --skip-mmsdm       # recent layer only

Output, one parquet per table under data/warehouse/:

    dispatchprice.parquet
    dispatchregionsum.parquet
    predispatchprice.parquet
    predispatchregionsum.parquet

Every row carries provenance:

    source        'mmsdm' | 'report_archive' | 'report_current'
    known_at      when the value became available — estimated for MMSDM rows
    known_at_kind 'estimated' | 'file_timestamp'
    ingested_at   when this row was written
    source_file
    vintage       unified run identifier (see VINTAGE_KEYS)
"""

import argparse
import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import nemweb as nw

ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = ROOT / "data" / "warehouse"
RAW_CACHE = ROOT / "data" / "raw"

START = "2023-01-01"
REGIONS = ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]

DISPATCH_TABLES = ["DISPATCHPRICE", "DISPATCHREGIONSUM"]
PREDISPATCH_TABLES = ["PREDISPATCHPRICE", "PREDISPATCHREGIONSUM"]

# Report files label the pre-dispatch tables differently from MMSDM.
REPORT_ALIAS = {
    "PREDISPATCHREGION_PRICES": "PREDISPATCHPRICE",
    "PREDISPATCHREGION_SOLUTION": "PREDISPATCHREGIONSUM",
}

REPORT_PATHS = {
    "DISPATCH": [
        "Reports/Current/DispatchIS_Reports",
        "Reports/Archive/DispatchIS_Reports",
    ],
    "PREDISPATCH": [
        "Reports/Current/PredispatchIS_Reports",
        "Reports/Archive/PredispatchIS_Reports",
    ],
}

KEEP = {
    "DISPATCHPRICE": [
        "SETTLEMENTDATE", "REGIONID", "INTERVENTION", "RRP", "PRICE_STATUS"],
    "DISPATCHREGIONSUM": [
        "SETTLEMENTDATE", "REGIONID", "INTERVENTION", "TOTALDEMAND",
        "AVAILABLEGENERATION", "AVAILABLELOAD", "DEMANDFORECAST",
        "NETINTERCHANGE", "INITIALSUPPLY", "UIGF", "SEMISCHEDULE_CLEAREDMW"],
    "PREDISPATCHPRICE": [
        "PREDISPATCHSEQNO", "RUN_DATETIME", "DATETIME", "REGIONID",
        "INTERVENTION", "RRP", "LASTCHANGED"],
    "PREDISPATCHREGIONSUM": [
        "PREDISPATCHSEQNO", "RUN_DATETIME", "DATETIME", "REGIONID",
        "INTERVENTION", "TOTALDEMAND", "AVAILABLEGENERATION", "AVAILABLELOAD",
        "UIGF", "SS_SOLAR_UIGF", "SS_WIND_UIGF", "LASTCHANGED"],
}

# MMSDM and report files identify a forecast run differently. Coalesce in this
# order into one `vintage` column so a single dedup key serves both sources.
VINTAGE_KEYS = ["RUN_DATETIME", "PREDISPATCHSEQNO", "LASTCHANGED"]


# ------------------------------------------------------------------ helpers

def stamp(df, source, known_at, source_file, kind):
    """Attach provenance columns."""
    df = df.copy()
    df["source"] = source
    df["known_at"] = known_at
    df["known_at_kind"] = kind
    df["ingested_at"] = pd.Timestamp.utcnow().tz_localize(None)
    df["source_file"] = source_file
    return df


def tidy(df, table):
    """Subset columns and coerce types."""
    for c in ("SETTLEMENTDATE", "DATETIME", "LASTCHANGED", "RUN_DATETIME"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")

    if "INTERVENTION" in df.columns:
        df["INTERVENTION"] = pd.to_numeric(df["INTERVENTION"], errors="coerce")

    if "REGIONID" in df.columns:
        df = df[df["REGIONID"].isin(REGIONS)]

    cols = [c for c in KEEP.get(table, df.columns) if c in df.columns]
    return df[cols]


def last_mmsdm_month():
    """Latest month with a published MMSDM archive."""
    year = datetime.now().year
    try:
        names = nw.list_dir(f"Data_Archive/Wholesale_Electricity/MMSDM/{year}")
    except Exception:
        names = []
    months = [int(m.group(1)) for n in names
              if (m := re.search(rf"MMSDM_{year}_(\d{{2}})", n))]
    if not months:
        return pd.Timestamp(year - 1, 12, 1)
    return pd.Timestamp(year, max(months), 1)


# ------------------------------------------------------------------ MMSDM

def backfill_mmsdm(start, end):
    """Dispatch tables via nemosis; pre-dispatch from the existing extracts."""
    from nemosis import dynamic_data_compiler

    out = {}
    RAW_CACHE.mkdir(parents=True, exist_ok=True)

    for table in DISPATCH_TABLES:
        print(f"  mmsdm {table} ...")
        df = dynamic_data_compiler(
            start.strftime("%Y/%m/%d 00:00:00"),
            end.strftime("%Y/%m/%d 00:00:00"),
            table, str(RAW_CACHE),
            filter_cols=["REGIONID"], filter_values=(REGIONS,))
        df = tidy(df, table)
        # MMSDM gives final values; estimate publication as interval end + 5 min.
        known = df["SETTLEMENTDATE"] + pd.Timedelta(minutes=5)
        out[table] = stamp(df, "mmsdm", known, "mmsdm", "estimated")
        print(f"    {len(df):,} rows")

    for table in PREDISPATCH_TABLES:
        folder = "predispatch" if table.endswith("PRICE") else "predispatch_regionsum"
        path = ROOT / "data" / "interim" / folder
        files = sorted(path.glob("*.parquet")) if path.exists() else []
        if not files:
            print(f"  mmsdm {table}: no extract found, skipping "
                  f"(run fetch_predispatch*.py first)")
            continue
        print(f"  mmsdm {table} from {len(files)} monthly extracts ...")
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        df = df.rename(columns={"PREDISPATCH_RUN_DATETIME": "RUN_DATETIME"})
        known = df["RUN_WRITTEN_AT"] if "RUN_WRITTEN_AT" in df.columns else None
        if known is None or known.isna().all():
            known = df["RUN_DATETIME"] - pd.Timedelta(minutes=30)
        df = tidy(df, table)
        out[table] = stamp(df, "mmsdm", known, "mmsdm", "estimated")
        print(f"    {len(df):,} rows")

    return out


# ------------------------------------------------------------------ reports

def backfill_reports(start, end, group, tables):
    """Walk report directories and parse everything in the window."""
    frames = {t: [] for t in tables}
    inverse = {v: k for k, v in REPORT_ALIAS.items()}
    request = {inverse.get(t, t) for t in tables}

    for path in REPORT_PATHS[group]:
        try:
            names = nw.list_dir(path)
        except Exception as exc:
            print(f"  {path}: {exc}")
            continue

        tier = "report_current" if "Current" in path else "report_archive"
        picked = []
        for n in names:
            ts = nw.file_datetime(n)
            if ts is None:
                m = re.search(r"_(\d{8})", n)          # ARCHIVE daily bundles
                ts = datetime.strptime(m.group(1), "%Y%m%d") if m else None
            if ts and start <= pd.Timestamp(ts) < end:
                picked.append((n, ts))

        print(f"  {path}: {len(picked)} files in window")
        for i, (name, ts) in enumerate(sorted(picked, key=lambda x: x[1]), 1):
            try:
                blocks = nw.fetch_report(path, name, request)
            except Exception as exc:
                print(f"    FAIL {name}: {exc}")
                continue
            for table, df in blocks.items():
                table = REPORT_ALIAS.get(table, table)
                if table not in frames:
                    continue
                df = tidy(df, table)
                if len(df):
                    frames[table].append(
                        stamp(df, tier, pd.Timestamp(ts), name, "file_timestamp"))
            if i % 50 == 0:
                print(f"    {i}/{len(picked)}")

    return {t: pd.concat(v, ignore_index=True) for t, v in frames.items() if v}


# ------------------------------------------------------------------ write

def add_vintage(df):
    """Coalesce the source-specific run identifiers into one column.

    MMSDM pre-dispatch rows carry RUN_DATETIME; report rows carry
    PREDISPATCHSEQNO. Keying on either alone collapses the other source's
    vintages into a single row.
    """
    present = [c for c in VINTAGE_KEYS if c in df.columns]
    if not present:
        return df

    vintage = pd.Series(pd.NA, index=df.index, dtype=object)
    for c in present:
        vintage = vintage.fillna(df[c].astype(object))

    if vintage.notna().any():
        df = df.copy()
        df["vintage"] = vintage.astype(str)
    return df


def merge_write(table, parts):
    """Union sources, drop exact duplicates, write."""
    parts = [p for p in parts if p is not None and len(p)]
    if not parts:
        print(f"  {table}: nothing to write")
        return

    df = add_vintage(pd.concat(parts, ignore_index=True))

    key = ["REGIONID"]
    key += ["SETTLEMENTDATE"] if "SETTLEMENTDATE" in df.columns else ["DATETIME"]
    if "vintage" in df.columns:
        key.append("vintage")
    if "INTERVENTION" in df.columns:
        key.append("INTERVENTION")      # keep both the physical and pricing runs
    key.append("source")

    before = len(df)
    df = df.drop_duplicates(key).sort_values(key).reset_index(drop=True)

    WAREHOUSE.mkdir(parents=True, exist_ok=True)
    dest = WAREHOUSE / f"{table.lower()}.parquet"
    df.to_parquet(dest, index=False)

    tcol = "SETTLEMENTDATE" if "SETTLEMENTDATE" in df.columns else "DATETIME"
    print(f"  {table}: {len(df):,} rows "
          f"({before - len(df):,} dupes dropped) "
          f"{df[tcol].min():%Y-%m-%d} -> {df[tcol].max():%Y-%m-%d}")
    print(f"    key: {key}")
    print(f"    by source: {df.source.value_counts().to_dict()}")


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=START)
    ap.add_argument("--skip-mmsdm", action="store_true")
    args = ap.parse_args()

    start = pd.Timestamp(args.start)
    today = pd.Timestamp.now().normalize()

    cutoff = last_mmsdm_month() + pd.offsets.MonthBegin(1)
    print(f"MMSDM published through {cutoff - pd.Timedelta(days=1):%Y-%m-%d}")
    print(f"report layer covers {cutoff:%Y-%m-%d} -> {today:%Y-%m-%d}\n")

    collected = {t: [] for t in DISPATCH_TABLES + PREDISPATCH_TABLES}

    if not args.skip_mmsdm:
        print("MMSDM layer")
        for t, df in backfill_mmsdm(start, cutoff).items():
            collected[t].append(df)
        print()

    print("report layer — dispatch")
    for t, df in backfill_reports(cutoff, today + timedelta(days=1),
                                  "DISPATCH", DISPATCH_TABLES).items():
        collected[t].append(df)

    print("\nreport layer — pre-dispatch")
    for t, df in backfill_reports(cutoff, today + timedelta(days=1),
                                  "PREDISPATCH", PREDISPATCH_TABLES).items():
        collected[t].append(df)

    print("\nwriting")
    for table, parts in collected.items():
        merge_write(table, parts)


if __name__ == "__main__":
    main()
