import time, yaml, requests
from pathlib import Path
import pandas as pd

cfg = yaml.safe_load(open("config.yaml"))
raw = Path(cfg["raw_dir"])
months = pd.date_range(cfg["start"], cfg["end"], freq="MS").strftime("%Y%m")

session = requests.Session()
session.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/csv,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.aemo.com.au/energy-systems/electricity/national-electricity-market-nem/data-nem/aggregated-data",
    "Connection": "keep-alive",
})

ok = fail = skip = 0

for region in cfg["regions"]:
    (raw / region).mkdir(parents=True, exist_ok=True)
    for ym in months:
        dest = raw / region / f"PRICE_AND_DEMAND_{ym}_{region}.csv"
        if dest.exists():
            skip += 1
            continue
        url = cfg["base_url"].format(ym=ym, region=region)
        try:
            r = session.get(url, timeout=60)
            if r.ok and r.text.startswith("REGION"):
                dest.write_text(r.text)
                ok += 1
                print(f"ok   {dest.name}")
            else:
                fail += 1
                print(f"FAIL {dest.name} [{r.status_code}]")
        except Exception as e:
            fail += 1
            print(f"ERR  {dest.name} {e}")
        time.sleep(cfg["request_delay"])

print(f"\ndownloaded {ok}, skipped {skip}, failed {fail}")