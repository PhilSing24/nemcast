"""Build the NEMCast raw layer: MMSDM archives plus the recent report gap.

AEMO publishes MMSDM monthly, roughly a week after month end, so between one and five
weeks of recent data exists only as report files. This covers both, writing to one
schema so the daily job appends to the same tables.

    python src/backfill.py                    # everything from START to today
    python src/backfill.py --skip-mmsdm       # recent layer only
    python src/backfill.py --years 2025 2026  # rebuild specific years

Output is partitioned by year under data/warehouse/:

    dispatchprice/2023.parquet, 2024.parquet, ...
    predispatchprice/2023.parquet, ...

Partitioning bounds memory — 17.7M pre-dispatch rows across 40 columns will not fit
in one frame. It is also safe: the dedup key includes the interval timestamp, so
duplicates always land in the same year and deduplicating within a partition is
equivalent to deduplicating globally.

Every row carries provenance:

    source        'mmsdm' | 'report_archive' | 'report_current'
    known_at      when the value became available — estimated for MMSDM rows
    known_at_kind 'estimated' | 'file_timestamp'
    ingested_at   when this row was written
    source_file
    vintage       unified run identifier
"""

import argparse
import gc
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
ALL_TABLES = DISPATCH_TABLES + PREDISPATCH_TABLES

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

FCAS_RRP = ["RAISE6SECRRP", "RAISE60SECRRP", "RAISE5MINRRP", "RAISEREGRRP",
            "LOWER6SECRRP", "LOWER60SECRRP", "LOWER5MINRRP", "LOWERREGRRP",
            "RAISE1SECRRP", "LOWER1SECRRP"]

# RRP1..RRP8 are prices under alternative demand scenarios — the spread across
# them measures how fragile an interval is.
SENSITIVITIES = [f"RRP{i}" for i in range(1, 9)]

KEEP = {
    "DISPATCHPRICE": [
        "SETTLEMENTDATE", "REGIONID", "INTERVENTION", "RRP", "PRICE_STATUS",
        "APCFLAG", "MARKETSUSPENDEDFLAG"] + FCAS_RRP,

    "DISPATCHREGIONSUM": [
        "SETTLEMENTDATE", "REGIONID", "INTERVENTION", "TOTALDEMAND",
        "AVAILABLEGENERATION", "AVAILABLELOAD", "DEMANDFORECAST",
        "DISPATCHABLEGENERATION", "DISPATCHABLELOAD", "NETINTERCHANGE",
        "EXCESSGENERATION", "INITIALSUPPLY", "CLEAREDSUPPLY", "UIGF",
        "SEMISCHEDULE_CLEAREDMW", "SEMISCHEDULE_COMPLIANCEMW",
        "TOTALINTERMITTENTGENERATION"],

    "PREDISPATCHPRICE": [
        "PREDISPATCHSEQNO", "RUN_DATETIME", "DATETIME", "REGIONID",
        "INTERVENTION", "RRP", "EEP", "LASTCHANGED"] + SENSITIVITIES,

    "PREDISPATCHREGIONSUM": [
        "PREDISPATCHSEQNO", "RUN_DATETIME", "DATETIME", "REGIONID",
        "INTERVENTION", "TOTALDEMAND", "AVAILABLEGENERATION", "AVAILABLELOAD",
        "DEMANDFORECAST", "NETINTERCHANGE", "EXCESSGENERATION",
        "INITIALSUPPLY", "CLEAREDSUPPLY", "UIGF", "SS_SOLAR_UIGF",
        "SS_WIND_UIGF", "SEMISCHEDULE_CLEAREDMW", "LASTCHANGED"],
}

# MMSDM and report files identify a run differently. Coalesce in this order.
VINTAGE_KEYS = ["RUN_DATETIME", "PREDISPATCHSEQNO", "LASTCHANGED"]

TIME_COL = {
    "DISPATCHPRICE": "SETTLEMENTDATE",
    "DISPATCHREGIONSUM": "SETTLEMENTDATE",
    "PREDISPATCHPRICE": "DATETIME",
    "PREDISPATCHREGIONSUM": "DATETIME",
}


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


def known_at(df, published, fallback):
    """Per-row publication time, falling back to the listed file's timestamp.

    tidy() drops rows but keeps the index, so the published series is realigned
    on it rather than assumed to be the same length.
    """
    if published is None:
        return pd.Series(pd.Timestamp(fallback), index=df.index)
    return pd.to_datetime(published.loc[df.index]).fillna(pd.Timestamp(fallback))


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


def add_vintage(df):
    """Coalesce source-specific run identifiers into one column.

    Works on string Series rather than object dtype — the object-based fillna
    version roughly doubles memory on 17M rows and was the cause of an OOM.
    """
    present = [c for c in VINTAGE_KEYS if c in df.columns]
    if not present:
        return df

    vintage = df[present[0]].astype(str)
    missing = {"nan", "NaT", "None", "", "<NA>"}
    for c in present[1:]:
        mask = vintage.isin(missing)
        if mask.any():
            vintage.loc[mask] = df.loc[mask, c].astype(str)
    df["vintage"] = vintage
    return df


def dedup_key(df):
    key = ["REGIONID"]
    key += ["SETTLEMENTDATE"] if "SETTLEMENTDATE" in df.columns else ["DATETIME"]
    if "vintage" in df.columns:
        key.append("vintage")
    if "INTERVENTION" in df.columns:
        key.append("INTERVENTION")
    key.append("source")
    return key


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


# ------------------------------------------------------------------ storage

def part_path(table, year):
    return WAREHOUSE / table.lower() / f"{year}.parquet"


def read_part(table, year):
    p = part_path(table, year)
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def write_part(table, year, df, label=""):
    """Merge into the year partition, deduplicate, write."""
    if df is None or df.empty:
        return 0

    df = add_vintage(df)
    old = read_part(table, year)
    combined = pd.concat([old, df], ignore_index=True) if len(old) else df

    key = dedup_key(combined)
    before = len(combined)
    combined = combined.drop_duplicates(key).sort_values(key).reset_index(drop=True)
    added = len(combined) - len(old)

    p = part_path(table, year)
    p.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(p, index=False)

    print(f"    {table} {year}: +{added:,} -> {len(combined):,} rows"
          f" ({before - len(combined):,} dupes){label}")

    del old, combined
    gc.collect()
    return added


def write_by_year(table, df, label=""):
    """Split a frame by year and write each partition separately."""
    if df is None or df.empty:
        return
    tcol = TIME_COL[table]
    df = df[df[tcol].notna()]
    for year, chunk in df.groupby(df[tcol].dt.year):
        write_part(table, int(year), chunk, label)


# ------------------------------------------------------------------ MMSDM

def mmsdm_dispatch(table, start, end):
    """One table via nemosis, written year by year."""
    from nemosis import dynamic_data_compiler

    print(f"  {table} via nemosis ...")
    RAW_CACHE.mkdir(parents=True, exist_ok=True)
    df = dynamic_data_compiler(
        start.strftime("%Y/%m/%d 00:00:00"),
        end.strftime("%Y/%m/%d 00:00:00"),
        table, str(RAW_CACHE),
        filter_cols=["REGIONID"], filter_values=(REGIONS,))
    df = tidy(df, table)
    # MMSDM holds final values; estimate publication as interval end + 5 min.
    known = df["SETTLEMENTDATE"] + pd.Timedelta(minutes=5)
    df = stamp(df, "mmsdm", known, "mmsdm", "estimated")
    write_by_year(table, df)
    del df
    gc.collect()


MMSDM_PREDISP = ("Data_Archive/Wholesale_Electricity/MMSDM/{y}/MMSDM_{y}_{m:02d}/"
                 "MMSDM_Historical_Data_SQLLoader/PREDISP_ALL_DATA")

EXTRACT = {
    "PREDISPATCHPRICE": ("predispatch", "predispatch_price"),
    "PREDISPATCHREGIONSUM": ("predispatch_regionsum", "predispatch_regionsum"),
}


def extract_path(table, year, month):
    folder, prefix = EXTRACT[table]
    return ROOT / "data" / "interim" / folder / f"{prefix}_{year}{month:02d}.parquet"


def fetch_mmsdm_predispatch(table, year, month):
    """Download one month of a pre-dispatch table from MMSDM and cache it.

    Matches the table by name in the directory listing rather than building a
    filename, so both conventions work: PUBLIC_DVD_PREDISPATCHPRICE_... before
    August 2024, PUBLIC_ARCHIVE#PREDISPATCHPRICE#ALL#FILE01#... after. Large
    months are split across FILE01, FILE02 — all parts are taken.

    The match is anchored so PREDISPATCHPRICE does not also catch
    PREDISPATCHPRICE_D or PREDISPATCHPRICESENSITIVITIES.
    """
    path = MMSDM_PREDISP.format(y=year, m=month)
    try:
        names = nw.list_dir(path)
    except Exception as exc:
        print(f"    {table} {year}-{month:02d}: cannot list archive ({exc})")
        return False

    pattern = re.compile(rf"(?:%23|#|_DVD_){table}(?:%23|#|_)(?:ALL|\d)", re.I)
    parts = sorted(n for n in names if pattern.search(n))
    if not parts:
        print(f"    {table} {year}-{month:02d}: not in the archive yet")
        return False

    # Inside the file the table carries its report label — PREDISPATCH,
    # REGION_PRICES — not the MMSDM name. Accept both, via the same alias map
    # the report layer uses.
    labels = {table} | {k for k, v in REPORT_ALIAS.items() if v == table}
    frames, found = [], set()
    for name in parts:
        blocks = nw.fetch_report(path, name, labels)
        for label, block in blocks.items():
            frames.append(block.drop(columns=[nw.PUBLISHED], errors="ignore"))
            found.add(label)
    if not frames:
        print(f"    {table} {year}-{month:02d}: files held none of {sorted(labels)}")
        return False

    df = pd.concat(frames, ignore_index=True)
    for c in ("DATETIME", "LASTCHANGED"):
        df[c] = pd.to_datetime(df[c], errors="coerce")

    # Same derivation as the extracts built for earlier months, so vintages are
    # comparable across the boundary: a run is labelled by the first interval it
    # covers, and LASTCHANGED is when it was actually written.
    df["PREDISPATCH_RUN_DATETIME"] = df.groupby("LASTCHANGED")["DATETIME"].transform("min")
    df["RUN_WRITTEN_AT"] = df["LASTCHANGED"]

    dest = extract_path(table, year, month)
    dest.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(dest, index=False)
    print(f"    {table} {year}-{month:02d}: fetched {len(df):,} rows from MMSDM "
          f"(labelled {', '.join(sorted(found))})")
    return True


def mmsdm_predispatch(table, years, cutoff):
    """Load pre-dispatch from monthly MMSDM extracts, one year at a time.

    Any month before the cutoff without a cached extract is fetched first. That
    matters because the cutoff moves: each time AEMO publishes a month, the
    report layer stops covering it, so if nothing fetched the new MMSDM month it
    would silently vanish — which is what happened to August 2026.

    Processing a year at a time bounds memory: all months at once, across ~30
    columns, does not fit on a modest machine.
    """
    for year in years:
        for month in range(1, 13):
            first = pd.Timestamp(year, month, 1)
            if first >= cutoff:
                break
            if not extract_path(table, year, month).exists():
                fetch_mmsdm_predispatch(table, year, month)

        files = sorted(extract_path(table, year, 1).parent.glob(f"*_{year}??.parquet"))
        if not files:
            continue
        print(f"  {table} {year}: {len(files)} monthly extracts ...")
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        df = df.rename(columns={"PREDISPATCH_RUN_DATETIME": "RUN_DATETIME"})

        # Per row, not per year: the old rule only fell back when EVERY value
        # was missing, so a year mixing months with and without a write time
        # (2024) kept its nulls.
        estimate = df["RUN_DATETIME"] - pd.Timedelta(minutes=30)
        known = (df["RUN_WRITTEN_AT"].fillna(estimate)
                 if "RUN_WRITTEN_AT" in df.columns else estimate)

        df = tidy(df, table)
        df = stamp(df, "mmsdm", known, "mmsdm", "estimated")
        write_by_year(table, df)
        del df
        gc.collect()


# ------------------------------------------------------------------ reports

def backfill_reports(start, end, group, tables):
    """Walk report directories and write what falls in the window."""
    inverse = {v: k for k, v in REPORT_ALIAS.items()}
    request = {inverse.get(t, t) for t in tables}
    buffer = {t: [] for t in tables}
    FLUSH = 100          # files between writes, to bound memory

    def flush():
        for table, parts in buffer.items():
            if parts:
                write_by_year(table, pd.concat(parts, ignore_index=True))
                parts.clear()
        gc.collect()

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
                if table not in buffer:
                    continue
                published = df.pop(nw.PUBLISHED) if nw.PUBLISHED in df.columns else None
                df = tidy(df, table)
                if len(df):
                    buffer[table].append(
                        stamp(df, tier, known_at(df, published, ts), name,
                              "file_timestamp"))
            if i % FLUSH == 0:
                print(f"    {i}/{len(picked)}")
                flush()
        flush()


# ------------------------------------------------------------------ summary

def summarise():
    print("\nwarehouse")
    for table in ALL_TABLES:
        parts = sorted((WAREHOUSE / table.lower()).glob("*.parquet")) \
            if (WAREHOUSE / table.lower()).exists() else []
        if not parts:
            print(f"  {table}: empty")
            continue
        total, spans, sources = 0, [], {}
        for p in parts:
            df = pd.read_parquet(p, columns=[TIME_COL[table], "source"])
            total += len(df)
            spans.append((df[TIME_COL[table]].min(), df[TIME_COL[table]].max()))
            for k, v in df.source.value_counts().items():
                sources[k] = sources.get(k, 0) + int(v)
            del df
        lo = min(s[0] for s in spans)
        hi = max(s[1] for s in spans)
        print(f"  {table}: {total:,} rows across {len(parts)} partitions "
              f"{lo:%Y-%m-%d} -> {hi:%Y-%m-%d}")
        print(f"    by source: {sources}")
    gc.collect()


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=START)
    ap.add_argument("--skip-mmsdm", action="store_true")
    ap.add_argument("--years", nargs="*", type=int,
                    help="restrict the MMSDM layer to these years")
    args = ap.parse_args()

    start = pd.Timestamp(args.start)
    today = pd.Timestamp.now().normalize()

    cutoff = last_mmsdm_month() + pd.offsets.MonthBegin(1)
    print(f"MMSDM published through {cutoff - pd.Timedelta(days=1):%Y-%m-%d}")
    print(f"report layer covers {cutoff:%Y-%m-%d} -> {today:%Y-%m-%d}\n")

    years = args.years or list(range(start.year, cutoff.year + 1))

    if not args.skip_mmsdm:
        print("MMSDM layer")
        for table in DISPATCH_TABLES:
            mmsdm_dispatch(table, start, cutoff)
        for table in PREDISPATCH_TABLES:
            mmsdm_predispatch(table, years, cutoff)
        print()

    print("report layer — dispatch")
    backfill_reports(cutoff, today + timedelta(days=1), "DISPATCH", DISPATCH_TABLES)

    print("\nreport layer — pre-dispatch")
    backfill_reports(cutoff, today + timedelta(days=1),
                     "PREDISPATCH", PREDISPATCH_TABLES)

    summarise()


if __name__ == "__main__":
    main()
