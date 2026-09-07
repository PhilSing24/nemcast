"""Load NEM reference data as tidy DataFrames.

    from nem_reference import load, regions_df, mix_df, sectors_df

    ref = load()
    mix_df(include_total=True)   # long: region_id, fuel, pct, label, colour
"""

import json
from functools import lru_cache
from pathlib import Path

import pandas as pd

PATH = Path(__file__).parent.parent / "data" / "reference" / "nem_reference.json"


@lru_cache(maxsize=1)
def load(path=None):
    with open(path or PATH) as fh:
        return json.load(fh)


def _rows(include_total):
    ref = load()
    rows = list(ref["regions"])
    if include_total:
        rows = rows + [ref["nem_total"]]
    return rows


def regions_df(include_total=False):
    """One row per region: population, demand, load factor, renewable share."""
    rows = []
    for r in _rows(include_total):
        rows.append({
            "region_id": r["id"],
            "name": r["name"],
            "short": r.get("short", "NEM"),
            "population_m": r["population_m"],
            "annual_demand_twh": r["annual_demand_twh"],
            "peak_demand_gw": r["peak_demand_gw"],
            "load_factor": r["load_factor"],
            "renewable_pct": r["renewable_pct"],
            "note": r.get("note", ""),
        })
    return pd.DataFrame(rows)


def cities_df():
    """One row per city."""
    rows = []
    for r in load()["regions"]:
        for rank, c in enumerate(r["cities"], start=1):
            rows.append({"region_id": r["id"], "rank": rank,
                         "city": c["name"], "population": c["population"]})
    return pd.DataFrame(rows)


def _long(key, meta_key, value_name, include_total):
    meta = {m["key"]: m for m in load()[meta_key]}
    rows = []
    for r in _rows(include_total):
        for k, v in r[key].items():
            rows.append({
                "region_id": r["id"],
                meta_key[:-1]: k,
                "label": meta[k]["label"],
                "colour": meta[k]["colour"],
                value_name: v,
            })
    return pd.DataFrame(rows)


def mix_df(include_total=False):
    """Long-format generation mix, ordered as in the reference file."""
    return _long("generation_mix_pct", "fuels", "pct", include_total)


def sectors_df(include_total=False):
    """Long-format consumption by sector."""
    return _long("consumption_pct", "sectors", "pct", include_total)


def interconnectors_df():
    return pd.DataFrame(load()["interconnectors"])


def facts():
    return load()["market_facts"]


def colours(kind="fuels"):
    """Mapping of label -> hex, for passing to a plotting library."""
    return {m["label"]: m["colour"] for m in load()[kind]}


if __name__ == "__main__":
    print(regions_df(include_total=True).to_string(index=False), "\n")
    print(mix_df().pivot(index="region_id", columns="label", values="pct"), "\n")
    print(cities_df().to_string(index=False))
