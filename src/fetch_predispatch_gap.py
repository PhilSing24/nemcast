"""Fetch pre-dispatch tables for months nemseer cannot reach.

From August 2024, AEMO renamed the archive files from
    PUBLIC_DVD_PREDISPATCHPRICE_YYYYMM010000.zip
to
    PUBLIC_ARCHIVE#PREDISPATCHPRICE#ALL#FILE01#YYYYMM010000.zip

nemseer's last release predates the change and 404s on these months. The files
are reachable; only the URL construction differs.

Output is written to match the nemseer months so the two sets concatenate:
  PREDISPATCH_RUN_DATETIME  run label = first interval the run forecasts
  RUN_WRITTEN_AT            LASTCHANGED, i.e. when the record was written
                            (absent from the nemseer months, hence NaT there)
"""

import io
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests

BASE = (
    "https://nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/"
    "{y}/MMSDM_{y}_{m:02d}/MMSDM_Historical_Data_SQLLoader/PREDISP_ALL_DATA/"
    "PUBLIC_ARCHIVE%23{table}%23ALL%23FILE{n:02d}%23{y}{m:02d}010000.zip"
)

HEADERS = {"User-Agent": "Mozilla/5.0"}

START, END = "2024-08", "2026-07"

TABLES = {
    "PREDISPATCHPRICE": {
        "out": "predispatch",
        "prefix": "predispatch_price",
        "keep": [
            "PREDISPATCH_RUN_DATETIME", "RUN_WRITTEN_AT", "DATETIME",
            "REGIONID", "RRP", "INTERVENTION",
        ],
    },
    "PREDISPATCHREGIONSUM": {
        "out": "predispatch_regionsum",
        "prefix": "predispatch_regionsum",
        "keep": [
            "PREDISPATCH_RUN_DATETIME", "RUN_WRITTEN_AT", "DATETIME",
            "REGIONID", "INTERVENTION", "TOTALDEMAND", "AVAILABLEGENERATION",
            "AVAILABLELOAD", "UIGF", "SS_SOLAR_UIGF", "SS_WIND_UIGF",
        ],
    },
}


def parse_mms_csv(text):
    """MMS row format: C=comment, I=column names, D=data.

    Both I and D rows carry four leading fields (row type, table group,
    table name, version) before the real columns begin.
    """
    cols, rows = None, []
    for line in io.StringIO(text):
        if line.startswith("I,"):
            parsed = pd.read_csv(io.StringIO(line), header=None).iloc[0].tolist()
            cols = [c for c in parsed[4:] if isinstance(c, str)]
        elif line.startswith("D,"):
            rows.append(line)

    if cols is None or not rows:
        return pd.DataFrame()

    return pd.read_csv(
        io.StringIO("".join(rows)),
        header=None,
        names=["_rt", "_t1", "_t2", "_ver"] + cols,
        low_memory=False,
    ).drop(columns=["_rt", "_t1", "_t2", "_ver"])


def fetch_month(table, year, month):
    """A month may be split across FILE01, FILE02, ... — fetch until 404."""
    parts, n = [], 1
    while True:
        url = BASE.format(y=year, m=month, table=table, n=n)
        resp = requests.get(url, headers=HEADERS, timeout=300)
        if resp.status_code == 404:
            break
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            raw = z.read(z.namelist()[0]).decode("utf-8", errors="replace")
        parts.append(parse_mms_csv(raw))
        n += 1
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def tidy(df, keep):
    """Normalise types and derive the two run-time columns."""
    for col in ("DATETIME", "LASTCHANGED"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # nemseer labels a run by the first interval it forecasts, not by when it
    # was written. Reproduce that so the old and new months are comparable.
    df["PREDISPATCH_RUN_DATETIME"] = df.groupby("LASTCHANGED")["DATETIME"].transform("min")
    df["RUN_WRITTEN_AT"] = df["LASTCHANGED"]

    df["INTERVENTION"] = pd.to_numeric(df["INTERVENTION"], errors="coerce")
    df = df.loc[df["INTERVENTION"] == 0]

    return df[[c for c in keep if c in df.columns]]


def main():
    for table, cfg in TABLES.items():
        out_dir = Path("data/interim") / cfg["out"]
        out_dir.mkdir(parents=True, exist_ok=True)

        for period in pd.date_range(START, END, freq="MS"):
            dest = out_dir / f"{cfg['prefix']}_{period:%Y%m}.parquet"
            if dest.exists():
                print("skip", dest.name)
                continue

            df = fetch_month(table, period.year, period.month)
            if df.empty:
                print("MISSING", table, period.strftime("%Y-%m"))
                continue

            df = tidy(df, cfg["keep"])
            df.to_parquet(dest, index=False)
            print(period.strftime("%Y-%m"), table, df.shape)
            time.sleep(1)


if __name__ == "__main__":
    main()
