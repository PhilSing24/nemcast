from nemseer import compile_data
import pandas as pd
from pathlib import Path

KEEP = ["PREDISPATCH_RUN_DATETIME", "DATETIME", "REGIONID", "INTERVENTION",
        "TOTALDEMAND", "AVAILABLEGENERATION", "AVAILABLELOAD",
        "UIGF", "SS_SOLAR_UIGF", "SS_WIND_UIGF"]

out = Path("data/interim/predispatch_regionsum")
out.mkdir(parents=True, exist_ok=True)

for m in pd.date_range("2023-01", "2024-07", freq="MS"):
    end = m + pd.offsets.MonthEnd(1)
    dest = out / f"predispatch_regionsum_{m:%Y%m}.parquet"
    if dest.exists():
        print("skip", dest.name)
        continue
    d = compile_data(
        run_start=m.strftime("%Y/%m/%d 00:00:00"),
        run_end=end.strftime("%Y/%m/%d 00:00:00"),
        forecasted_start=m.strftime("%Y/%m/%d 00:00:00"),
        forecasted_end=(end + pd.Timedelta(days=1)).strftime("%Y/%m/%d 00:00:00"),
        forecast_type="PREDISPATCH",
        tables=["REGIONSUM"],
        raw_cache="data/raw_nemseer/",
    )
    df = d["REGIONSUM"]
    df = df.loc[df.INTERVENTION == 0, KEEP]
    df.to_parquet(dest, index=False)
    print(m.strftime("%Y-%m"), df.shape)