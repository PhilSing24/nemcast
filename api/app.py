"""NEMCast data API — serves the warehouse as a vendor would.

Plays the role of a data vendor: CaféBot's Custom API connector calls it with a
table and a date range, and gets the rows back. The store behind it is the
parquet files that src/daily.py keeps current. DuckDB reads them directly, so
there is no database server to run.

    NEMCAST_API_KEY=dev uvicorn api.app:app --reload

Endpoints
    GET /health                  liveness, and how fresh each table is
    GET /tables                  what is available: row counts and date ranges
    GET /data/{table}            rows for a date range — the one CaféBot calls

Every row keeps its provenance — source, known_at, vintage — so a consumer can
reconstruct what was knowable at any past moment. An API that returned only the
latest forecast would collapse ~55 runs per interval into one and lose that.

Times are NEM time (AEST, UTC+10, no daylight saving), returned without offset.
"""

import os
from datetime import date
from pathlib import Path
from typing import Literal, Optional

import duckdb
import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse

ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = Path(os.environ.get("NEMCAST_WAREHOUSE", ROOT / "data" / "warehouse"))
MAX_LIMIT = int(os.environ.get("NEMCAST_MAX_LIMIT", "50000"))

# table -> the column that places a row in time
TABLES = {
    "dispatchprice": "SETTLEMENTDATE",
    "dispatchregionsum": "SETTLEMENTDATE",
    "predispatchprice": "DATETIME",
    "predispatchregionsum": "DATETIME",
}

app = FastAPI(
    title="NEMCast data API",
    description="Australian NEM dispatch and pre-dispatch data, with provenance.",
    version="1.0",
)


# ------------------------------------------------------------------ helpers

def require_key(x_api_key: Optional[str] = Header(default=None)):
    """Every request needs the key, as a real vendor API would."""
    expected = os.environ.get("NEMCAST_API_KEY")
    if not expected:
        raise HTTPException(500, "server has no NEMCAST_API_KEY configured")
    if x_api_key != expected:
        raise HTTPException(401, "missing or invalid X-API-Key header")


def files(table: str) -> str:
    if table not in TABLES:
        raise HTTPException(404, f"unknown table '{table}'; see /tables")
    folder = WAREHOUSE / table
    if not any(folder.glob("*.parquet")):
        raise HTTPException(503, f"no data yet for '{table}'")
    return str(folder / "*.parquet")


def query(sql: str, params: list) -> pd.DataFrame:
    # A fresh connection per request: DuckDB is in-process and cheap to open,
    # and it picks up files daily.py has rewritten since the last call.
    with duckdb.connect() as con:
        return con.execute(sql, params).fetch_df()


def serialisable(df: pd.DataFrame) -> pd.DataFrame:
    """Timestamps to ISO strings, NaN to null — so JSON and CSV come out clean."""
    df = df.copy()
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            df[c] = df[c].dt.strftime("%Y-%m-%dT%H:%M:%S")
    return df.astype(object).where(df.notna(), None)


# ------------------------------------------------------------------ endpoints

@app.get("/health")
def health():
    """Unauthenticated, so a load balancer or monitor can call it."""
    fresh = {}
    for table, tcol in TABLES.items():
        folder = WAREHOUSE / table
        if not any(folder.glob("*.parquet")):
            fresh[table] = None
            continue
        latest = query(
            f"SELECT max({tcol}) AS t FROM read_parquet(?, union_by_name=true)",
            [str(folder / "*.parquet")]).t[0]
        fresh[table] = None if pd.isna(latest) else latest.isoformat()
    return {"status": "ok", "latest_interval": fresh}


@app.get("/tables", dependencies=[Depends(require_key)])
def tables():
    out = []
    for table, tcol in TABLES.items():
        folder = WAREHOUSE / table
        if not any(folder.glob("*.parquet")):
            continue
        s = query(
            f"""SELECT count(*) AS rows,
                       min({tcol}) AS first_interval,
                       max({tcol}) AS last_interval
                FROM read_parquet(?, union_by_name=true)""",
            [str(folder / "*.parquet")])
        out.append({
            "table": table,
            "time_column": tcol,
            "rows": int(s.rows[0]),
            "first_interval": s.first_interval[0].isoformat(),
            "last_interval": s.last_interval[0].isoformat(),
        })
    return out


@app.get("/data/{table}", dependencies=[Depends(require_key)])
def data(
    table: str,
    start: date = Query(..., description="first day, inclusive (NEM time)"),
    end: date = Query(..., description="last day, exclusive"),
    region: Optional[str] = Query(None, description="NSW1, QLD1, VIC1, SA1 or TAS1"),
    limit: int = Query(10000, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    format: Literal["json", "csv"] = Query("json"),
):
    """Rows whose interval falls in [start, end).

    Results are in a stable order, so paging with limit and offset is safe.
    A full page means there may be more: call again with the next offset.
    """
    if end <= start:
        raise HTTPException(400, "end must be after start")

    tcol = TABLES[table] if table in TABLES else None
    path = files(table)

    where = [f"{tcol} >= ?", f"{tcol} < ?"]
    params: list = [path, start, end]
    if region:
        where.append("REGIONID = ?")
        params.append(region.upper())

    # vintage is in the sort for forecast tables so every run keeps its place
    order = f"{tcol}, REGIONID" + (", vintage" if table.startswith("pre") else "")

    df = query(
        f"""SELECT * FROM read_parquet(?, union_by_name=true)
            WHERE {' AND '.join(where)}
            ORDER BY {order}
            LIMIT {limit} OFFSET {offset}""",
        params)

    df = serialisable(df)
    next_offset = offset + limit if len(df) == limit else None

    if format == "csv":
        headers = {"X-Next-Offset": str(next_offset)} if next_offset else {}
        return PlainTextResponse(df.to_csv(index=False),
                                 media_type="text/csv", headers=headers)

    return {
        "table": table,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "region": region,
        "count": len(df),
        "next_offset": next_offset,
        "data": df.to_dict(orient="records"),
    }
