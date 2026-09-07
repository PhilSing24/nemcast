"""Replacement for load_predispatch() and aggregate_run() in build_features.py.

Processes pre-dispatch month by month instead of loading all 17M rows at once.
Consecutive file pairs are read together so that runs spanning a month boundary
(a run on 31 Jan forecasting 1 Feb) are not lost; duplicates are dropped after.
"""

from pathlib import Path

import pandas as pd

INTERIM = Path("data/interim")
THRESHOLD = 300.0
TRADING_DAY_OFFSET = pd.Timedelta(hours=4, minutes=5)


def trading_day(ts):
    return (ts - TRADING_DAY_OFFSET).dt.normalize()


def _load_pair(price_files, rsum_files, i):
    """Read file i and i+1 together, merged."""
    keys = ["PREDISPATCH_RUN_DATETIME", "DATETIME", "REGIONID"]
    rsum_cols = ["TOTALDEMAND", "AVAILABLEGENERATION", "AVAILABLELOAD",
                 "SS_SOLAR_UIGF", "SS_WIND_UIGF"]

    idx = [i] if i + 1 >= len(price_files) else [i, i + 1]

    price = pd.concat([pd.read_parquet(price_files[j], columns=keys + ["RRP"])
                       for j in idx], ignore_index=True)
    rsum = pd.concat([pd.read_parquet(rsum_files[j], columns=keys + rsum_cols)
                      for j in idx], ignore_index=True)

    df = price.merge(rsum, on=keys, how="inner")
    del price, rsum

    df["run_day"] = df.PREDISPATCH_RUN_DATETIME.dt.normalize()
    df["run_time"] = df.PREDISPATCH_RUN_DATETIME - df.run_day
    df["target_day"] = trading_day(df.DATETIME)
    df["margin"] = df.AVAILABLEGENERATION - df.TOTALDEMAND
    return df


def _aggregate(df, cutoff, prefix):
    """Last run at or before cutoff, covering the next trading day."""
    sel = df[df.run_time <= cutoff]
    if sel.empty:
        return pd.DataFrame()

    latest = sel.groupby("run_day").PREDISPATCH_RUN_DATETIME.max().rename("chosen")
    sel = sel.merge(latest, on="run_day")
    sel = sel[sel.PREDISPATCH_RUN_DATETIME == sel.chosen]
    sel = sel[sel.target_day == sel.run_day + pd.Timedelta(days=1)]
    if sel.empty:
        return pd.DataFrame()

    agg = (sel.groupby(["REGIONID", "target_day"])
              .agg(**{
                  f"{prefix}_max_rrp": ("RRP", "max"),
                  f"{prefix}_mean_rrp": ("RRP", "mean"),
                  f"{prefix}_p90_rrp": ("RRP", lambda s: s.quantile(0.90)),
                  f"{prefix}_n_over_300": ("RRP", lambda s: (s > THRESHOLD).sum()),
                  f"{prefix}_n_negative": ("RRP", lambda s: (s < 0).sum()),
                  f"{prefix}_max_demand": ("TOTALDEMAND", "max"),
                  f"{prefix}_mean_demand": ("TOTALDEMAND", "mean"),
                  f"{prefix}_min_margin": ("margin", "min"),
                  f"{prefix}_mean_margin": ("margin", "mean"),
                  f"{prefix}_min_wind": ("SS_WIND_UIGF", "min"),
                  f"{prefix}_mean_wind": ("SS_WIND_UIGF", "mean"),
                  f"{prefix}_max_solar": ("SS_SOLAR_UIGF", "max"),
                  f"{prefix}_mean_load_bid": ("AVAILABLELOAD", "mean"),
                  f"{prefix}_n_periods": ("RRP", "size"),
              })
              .reset_index())
    return agg.rename(columns={"target_day": "trading_day"})


def build_forecast_features(deadline, morning):
    """Month-by-month equivalent of load_predispatch() + aggregate_run()."""
    price_files = sorted((INTERIM / "predispatch").glob("*.parquet"))
    rsum_files = sorted((INTERIM / "predispatch_regionsum").glob("*.parquet"))
    assert len(price_files) == len(rsum_files), "file counts differ"

    fc_parts, am_parts = [], []

    for i, f in enumerate(price_files):
        df = _load_pair(price_files, rsum_files, i)
        fc_parts.append(_aggregate(df, deadline, "fc"))
        am_parts.append(_aggregate(df, morning, "am"))
        del df
        print(f"  {f.stem[-6:]}  {len(fc_parts[-1]):>4} region-days")

    fc = (pd.concat(fc_parts, ignore_index=True)
            .drop_duplicates(["REGIONID", "trading_day"]))
    am = (pd.concat(am_parts, ignore_index=True)
            .drop_duplicates(["REGIONID", "trading_day"]))
    return fc, am
