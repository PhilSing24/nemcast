"""Build the NEMCast modelling table.

Target
------
At 12:30 on day D, for each NEM region: will RRP exceed $300 in at least one
5-minute dispatch interval during the trading day beginning 04:05 on D+1?

Features come from the last pre-dispatch run at or before 12:30 on D, plus a
morning run for revision features. Nothing published after 12:30 is used.

Output: data/features/nemcast_daily.parquet  (~6,400 rows)
"""

from pathlib import Path

import numpy as np
import pandas as pd

INTERIM = Path("data/interim")
OUT = Path("data/features")
OUT.mkdir(parents=True, exist_ok=True)

THRESHOLD = 300.0
TRADING_DAY_OFFSET = pd.Timedelta(hours=4, minutes=5)
REGIONS = ["NSW1", "QLD1", "SA1", "TAS1", "VIC1"]

# Runs are labelled by the first interval they cover, and written ~30 min
# earlier. A run labelled 13:00 was therefore written around 12:30, so the
# latest run safely available at the 12:30 deadline is the one labelled 12:30.
DEADLINE = pd.Timedelta(hours=13)
MORNING = pd.Timedelta(hours=20)


def trading_day(ts):
    """Map a timestamp to its trading day (which runs 04:05 -> 04:00)."""
    return (ts - TRADING_DAY_OFFSET).dt.normalize()


# ---------------------------------------------------------------- label

def build_label():
    px = pd.read_parquet(INTERIM / "dispatchprice.parquet",
                         columns=["SETTLEMENTDATE", "REGIONID", "RRP"])
    px["trading_day"] = trading_day(px.SETTLEMENTDATE)

    label = (px.groupby(["REGIONID", "trading_day"])
               .agg(actual_max_rrp=("RRP", "max"),
                    actual_mean_rrp=("RRP", "mean"),
                    actual_min_rrp=("RRP", "min"),
                    n_intervals=("RRP", "size"))
               .reset_index())

    # Drop partial days at the series edges.
    label = label[label.n_intervals == 288].drop(columns="n_intervals")
    label["spike"] = (label.actual_max_rrp > THRESHOLD).astype(int)
    return label


# ---------------------------------------------------------------- features

def load_predispatch():
    price = pd.concat(
        [pd.read_parquet(f) for f in sorted((INTERIM / "predispatch").glob("*.parquet"))],
        ignore_index=True)
    rsum = pd.concat(
        [pd.read_parquet(f) for f in sorted((INTERIM / "predispatch_regionsum").glob("*.parquet"))],
        ignore_index=True)

    keys = ["PREDISPATCH_RUN_DATETIME", "DATETIME", "REGIONID"]
    df = price[keys + ["RRP"]].merge(
        rsum[keys + ["TOTALDEMAND", "AVAILABLEGENERATION", "AVAILABLELOAD",
                     "SS_SOLAR_UIGF", "SS_WIND_UIGF"]],
        on=keys, how="inner")

    df["run_day"] = df.PREDISPATCH_RUN_DATETIME.dt.normalize()
    df["run_time"] = df.PREDISPATCH_RUN_DATETIME - df.run_day
    df["target_day"] = trading_day(df.DATETIME)
    df["margin"] = df.AVAILABLEGENERATION - df.TOTALDEMAND
    return df


def aggregate_run(df, cutoff, prefix):
    """For each run_day, take the last run at or before `cutoff`, keep the rows
    covering the next trading day, and aggregate to one row per region."""
    eligible = df[df.run_time <= cutoff]

    latest = (eligible.groupby("run_day")
                      .PREDISPATCH_RUN_DATETIME.max()
                      .rename("chosen_run"))
    sel = eligible.merge(latest, on="run_day")
    sel = sel[sel.PREDISPATCH_RUN_DATETIME == sel.chosen_run]

    # Keep only the trading day after the run day.
    sel = sel[sel.target_day == sel.run_day + pd.Timedelta(days=1)]

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


# ---------------------------------------------------------------- assemble

def main():
    print("building label...")
    label = build_label()
    print(f"  {len(label):,} region-days, spike rate {label.spike.mean():.1%}")

    print("loading pre-dispatch...")
    pdp = load_predispatch()
    print(f"  {len(pdp):,} forecast rows")

    print("aggregating 12:30 run...")
    f1230 = aggregate_run(pdp, DEADLINE, "fc")
    print(f"  {len(f1230):,} region-days")

    print("aggregating 06:30 run...")
    f0630 = aggregate_run(pdp, MORNING, "am")

    df = (label.merge(f1230, on=["REGIONID", "trading_day"], how="inner")
               .merge(f0630[["REGIONID", "trading_day", "am_max_rrp", "am_min_wind",
                             "am_max_demand"]],
                      on=["REGIONID", "trading_day"], how="left"))

    # Revision features: how the forecast moved through the morning.
    df["rev_max_rrp"] = df.fc_max_rrp - df.am_max_rrp
    df["rev_min_wind"] = df.fc_min_wind - df.am_min_wind
    df["rev_max_demand"] = df.fc_max_demand - df.am_max_demand

    # Calendar.
    df["dow"] = df.trading_day.dt.dayofweek
    df["month"] = df.trading_day.dt.month
    df["is_weekend"] = (df.dow >= 5).astype(int)
    df["season"] = np.select(
        [df.month.isin([12, 1, 2]), df.month.isin([6, 7, 8])],
        ["summer", "winter"], default="shoulder")

    # Drop days where the chosen run didn't cover the full target day.
    df = df[df.fc_n_periods >= 40].drop(columns=["fc_n_periods"])

    df = df.sort_values(["trading_day", "REGIONID"]).reset_index(drop=True)

    dest = OUT / "nemcast_daily.parquet"
    df.to_parquet(dest, index=False)

    print(f"\nwrote {dest}")
    print(f"  {df.shape[0]:,} rows x {df.shape[1]} cols")
    print(f"  {df.trading_day.min():%Y-%m-%d} -> {df.trading_day.max():%Y-%m-%d}")
    print(f"\nspike rate by region:")
    print(df.groupby("REGIONID").spike.agg(["mean", "sum", "size"]))


if __name__ == "__main__":
    main()
