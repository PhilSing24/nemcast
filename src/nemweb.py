"""Parsing and fetching helpers for AEMO's NEMWEB report files.

AEMO publishes the same data in two shapes:

  MMSDM archives   one file per table per month, published ~7 days after month end
  Report files     one file per dispatch or pre-dispatch run, published within minutes

Both use the MMS row format:

    C,...          comment — file metadata, first and last lines
    I,DISPATCH,PRICE,5,SETTLEMENTDATE,RUNNO,REGIONID,...     column names
    D,DISPATCH,PRICE,5,"2024/08/01 00:05:00",1,NSW1,...      data
    F,...          footer

The four leading fields on I and D rows are row type, table group, table name and
version. Real columns start at index 4. A single report file contains several tables,
each introduced by its own I row.
"""

import io
import re
import zipfile
from datetime import datetime

import pandas as pd
import requests

NEMWEB = "https://nemweb.com.au"
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 120


# ------------------------------------------------------------------ listing

def list_dir(path):
    """Return filenames in a NEMWEB directory.

    IIS listings use uppercase HREF and absolute paths.
    """
    url = f"{NEMWEB}/{path.strip('/')}/"
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    hrefs = re.findall(r'HREF="([^"]+)"', resp.text, flags=re.I)
    return [h.rsplit("/", 1)[-1] for h in hrefs if h.lower().endswith(".zip")]


def file_datetime(name):
    """Pull the run timestamp out of a report filename.

    PUBLIC_DISPATCHIS_202609092210_0000000536970282.zip -> 2026-09-09 22:10
    """
    m = re.search(r"_(\d{12})_", name)
    return datetime.strptime(m.group(1), "%Y%m%d%H%M") if m else None


# Column added to every parsed table, carrying when its source file was published.
# Callers pop it before writing and use it as known_at.
PUBLISHED = "_published"


def publication_time(name):
    """When a single report file became available, read from its own name.

    Two naming schemes, and they differ in what the numbers mean:

      PUBLIC_PREDISPATCHIS_202609092330_20260909230155.zip
          run label 23:30, published 23:01:55 — the trailing 14 digits are
          the actual publication time, so use them

      PUBLIC_DISPATCHIS_202609092210_0000000536970282.zip
          interval ending 22:10 — the trailing number is a sequence id, not
          a time. The file lands a few minutes BEFORE 22:10, so the interval
          stamp is a slightly late, conservative estimate. Never early.

    Returns None for names without a timestamp, such as archive day bundles.
    """
    m = re.search(r"_(\d{14})\.(?:zip|csv)$", name, flags=re.I)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    m = re.search(r"_(\d{12})(?:_|\.)", name)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M")
    return None


# ------------------------------------------------------------------ parsing

def parse_mms(text, tables=None):
    """Split MMS row-format text into one DataFrame per table.

    Returns {table_name: DataFrame}. `tables` filters to a subset, e.g.
    {"DISPATCHPRICE", "DISPATCHREGIONSUM"}.
    """
    blocks, cols, name, rows = {}, None, None, []

    def flush():
        if name and cols and rows:
            df = pd.read_csv(
                io.StringIO("".join(rows)), header=None,
                names=["_rt", "_g", "_t", "_v"] + cols, low_memory=False)
            blocks[name] = df.drop(columns=["_rt", "_g", "_t", "_v"])

    for line in io.StringIO(text):
        if line.startswith("I,"):
            flush()
            rows = []
            parsed = next(pd.read_csv(io.StringIO(line), header=None).itertuples(
                index=False, name=None))
            group, table = str(parsed[1]).strip(), str(parsed[2]).strip()
            name = f"{group}{table}".upper()
            cols = [c for c in parsed[4:] if isinstance(c, str)]
        elif line.startswith("D,") and name:
            rows.append(line)
    flush()

    if tables:
        wanted = {t.upper() for t in tables}
        blocks = {k: v for k, v in blocks.items() if k in wanted}
    return blocks


def read_zip(content, tables=None, _outer=None):
    """Parse every CSV in a zip, recursing into nested zips.

    Each parsed table gets a PUBLISHED column taken from the innermost file
    that carries a timestamp. That matters for archive day bundles such as
    PUBLIC_DISPATCHIS_20260919.zip: the bundle's own name only gives a date,
    and midnight on that date is BEFORE most of the data inside it existed.
    Stamping rows with it made values look known a day early — leakage.
    The 288 files inside each carry their own correct times.
    """
    out = {}
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        for member in z.namelist():
            data = z.read(member)
            if member.lower().endswith(".zip"):
                inner = read_zip(data, tables, _outer=member)
                for k, v in inner.items():
                    out.setdefault(k, []).append(v)
            elif member.upper().endswith((".CSV", ".TXT")):
                ts = publication_time(member) or publication_time(_outer or "")
                for k, v in parse_mms(
                        data.decode("utf-8", errors="replace"), tables).items():
                    v[PUBLISHED] = pd.Timestamp(ts) if ts else pd.NaT
                    out.setdefault(k, []).append(v)
    return {k: pd.concat(v, ignore_index=True) for k, v in out.items()}


def fetch_report(path, filename, tables=None):
    """Download one report zip and parse it."""
    url = f"{NEMWEB}/{path.strip('/')}/{filename}"
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return read_zip(resp.content, tables)
