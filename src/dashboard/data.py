"""Cached data access for the NEMCast dashboard.

Every loader is cached — Streamlit re-runs the whole script on each interaction,
so uncached parquet reads make the app unusable.
"""

import json
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
INTERIM = ROOT / "data" / "interim"
FEATURES = ROOT / "data" / "features"
REFERENCE = ROOT / "data" / "reference" / "nem_reference.json"

REGION_ORDER = ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]


@st.cache_data
def reference():
    with open(REFERENCE) as fh:
        return json.load(fh)


@st.cache_data
def regions_df(include_total=False):
    ref = reference()
    rows = list(ref["regions"]) + ([ref["nem_total"]] if include_total else [])
    return pd.DataFrame([{
        "region_id": r["id"],
        "name": r["name"],
        "short": r.get("short", "NEM"),
        "population_m": r["population_m"],
        "annual_demand_twh": r["annual_demand_twh"],
        "peak_demand_gw": r["peak_demand_gw"],
        "load_factor": r["load_factor"],
        "renewable_pct": r["renewable_pct"],
        "note": r.get("note", ""),
    } for r in rows])


@st.cache_data
def cities_df():
    rows = []
    for r in reference()["regions"]:
        for rank, c in enumerate(r["cities"], start=1):
            rows.append({"region_id": r["id"], "rank": rank,
                         "city": c["name"], "population": c["population"]})
    return pd.DataFrame(rows)


def _long(key, meta_key, include_total):
    ref = reference()
    meta = {m["key"]: m for m in ref[meta_key]}
    rows = list(ref["regions"]) + ([ref["nem_total"]] if include_total else [])
    out = []
    for r in rows:
        for k, v in r[key].items():
            out.append({"region_id": r["id"], "key": k,
                        "label": meta[k]["label"], "colour": meta[k]["colour"],
                        "pct": v})
    return pd.DataFrame(out)


@st.cache_data
def mix_df(include_total=False):
    return _long("generation_mix_pct", "fuels", include_total)


@st.cache_data
def sectors_df(include_total=False):
    return _long("consumption_pct", "sectors", include_total)


def colours(kind="fuels"):
    return {m["label"]: m["colour"] for m in reference()[kind]}


@st.cache_data
def prices(region=None):
    """5-minute dispatch prices. Filter early — this is 1.9M rows."""
    df = pd.read_parquet(INTERIM / "dispatchprice.parquet",
                         columns=["SETTLEMENTDATE", "REGIONID", "RRP"])
    if region:
        df = df[df.REGIONID == region]
    return df


@st.cache_data
def features():
    """The day-level modelling table, if it has been built."""
    path = FEATURES / "nemcast_daily.parquet"
    return pd.read_parquet(path) if path.exists() else None
