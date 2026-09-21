# NEMCast data API — handover

A small service that plays the role of a data vendor for the NEMCast use case.
CaféBot's **Custom API** connector calls it to pull Australian electricity market data.

Two processes, one image:

| Service | Does | Schedule |
|---|---|---|
| `ingest` | Fetches new data from AEMO and appends it to the warehouse | 04:30 and 20:30 NEM time, plus once on start-up |
| `api` | Serves the warehouse over HTTP to CaféBot | Always on, port 8000 |

The warehouse is a folder of parquet files on a mounted volume. There is no database
server to run. Only `ingest` writes to it; `api` mounts it read-only.

---

## What IT needs to provide

1. **A host that can run Docker Compose.** Modest: 2 CPU, 4 GB RAM, 5 GB disk.
2. **Outbound HTTPS to `nemweb.com.au`** — the only external dependency. Most likely
   to need a firewall exception, and the one to request first.
3. **Inbound access to port 8000 from the CaféBot server.** Nothing else needs to reach it.
4. **A secret** for `NEMCAST_API_KEY`.
5. **The seed data** — the existing `data/warehouse/` folder (~700 MB), copied onto the
   host. Without it, `ingest` only fetches the last two days AEMO keeps online.

---

## Deploy

```bash
cp .env.example .env          # set NEMCAST_API_KEY to a real secret
# copy the seed data into ./data/warehouse/
docker compose up -d --build
```

Check it:

```bash
curl http://localhost:8000/health
docker compose logs -f ingest
```

`/health` needs no key and reports the latest interval in each table. If those
timestamps stop advancing, ingestion has stalled.

---

## Connecting CaféBot

In CaféBot: **Data Pipeline → API → Custom API.**

| Field | Value |
|---|---|
| URL | `http://<host>:8000/data/dispatchprice` |
| Method | GET |
| Header | `X-API-Key: <the secret>` |
| Params | `start=2026-09-01`, `end=2026-09-02`, optionally `region=SA1` |

Add `format=csv` if the connector prefers delimited text to JSON.

The four tables are `dispatchprice`, `dispatchregionsum`, `predispatchprice` and
`predispatchregionsum`. `GET /tables` lists them with their date ranges.

**Date ranges are inclusive of `start`, exclusive of `end`,** on the timestamp as AEMO
publishes it. So consecutive daily requests cover every interval exactly once, with no
gaps and no double counting.

**Large ranges are paged.** Default 10,000 rows per call, up to 50,000. A full page means
there may be more — call again with `offset` set to the returned `next_offset`, or the
`X-Next-Offset` header in CSV mode.

Full interactive documentation is served at `http://<host>:8000/docs`.

---

## Operating it

**Normal:** two ingestion runs a day, each taking seconds to a couple of minutes.

**Exit codes in the `ingest` log:** `0` appended new data · `1` nothing new, which is
fine · `2` fetch or write error, which needs attention.

**If ingestion fails for a day or two,** it catches up on its own at the next run —
AEMO keeps two days of recent files online. **Beyond two days,** run a wider lookback:

```bash
docker compose exec ingest python src/daily.py --mode dispatch --lookback 10
docker compose exec ingest python src/daily.py --mode predispatch --lookback 10
```

**Times** are NEM time throughout: AEST, UTC+10, no daylight saving. The container
is set to `Australia/Brisbane`, which is the same clock. Do not change it — the
schedule and the ingestion window both depend on it.

**Backups:** the warehouse folder is the only state. Back it up; everything else is
rebuilt from the image.

---

## Security

- The key is required on every endpoint except `/health`.
- `.env` holds the secret and must never be committed.
- The API is read-only. Nothing a caller sends can modify the data.
- Only port 8000 needs to be exposed, and only to the CaféBot server.
