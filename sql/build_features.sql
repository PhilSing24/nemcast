-- NEMCast — from raw tables to the modelling dataset
--
--     ~/clickhouse client --host ::1 --multiquery < sql/build_features.sql
--
-- Four raw tables in, one row per region-day out. The result, nemcast.features,
-- is then split three ways: train, validation, test.
--
-- The whole file turns on one rule: a feature may only use information that was
-- available at the decision point. `known_at` enforces it — every row carries the
-- moment its value became knowable, so "as at 20:00 yesterday" is a filter rather
-- than an assumption.
--
--   Layer 1   v_price_final, v_regionsum_final, v_forecast   cleanup, same grain
--   Layer 2   v_daily_outcome, v_forecast_1300/2000          aggregation
--   Layer 3   features                                       the modelling table
--
-- Two conventions that are easy to get wrong:
--
--   The trading day runs 04:00 to 04:00, and timestamps mark the END of an
--   interval. The label 04:05 is the first interval of the day and 04:00 is the
--   last. Subtracting 245 minutes maps both to the right date.
--
--   The decision point is 20:00 on day D, forecasting the trading day that begins
--   04:05 on D+1 — a horizon of 8 to 32 hours.


-- ============================================================ layer 1
-- Cleanup only. Same grain as the raw tables, three rules applied once: keep the
-- pricing run, prefer the authoritative source, attach the trading day.

-- The same interval can arrive from three feeds. MMSDM is final and wins; the
-- report archive is next; the live capture is the fallback. Without this, the
-- August/September overlap shows up as duplicate intervals.
CREATE OR REPLACE VIEW nemcast.v_price_final AS
SELECT
    REGIONID                                        AS regionid,
    SETTLEMENTDATE                                  AS settlementdate,
    toDate(SETTLEMENTDATE - INTERVAL 245 MINUTE)    AS trading_day,
    RRP                                             AS rrp,
    PRICE_STATUS                                    AS price_status,
    source,
    known_at
FROM
(
    SELECT
        *,
        row_number() OVER (
            PARTITION BY REGIONID, SETTLEMENTDATE
            ORDER BY multiIf(source = 'mmsdm', 1,
                             source = 'report_archive', 2, 3)
        ) AS pick
    FROM nemcast.dispatchprice
    WHERE INTERVENTION = 0
)
WHERE pick = 1;


CREATE OR REPLACE VIEW nemcast.v_regionsum_final AS
SELECT
    REGIONID                                        AS regionid,
    SETTLEMENTDATE                                  AS settlementdate,
    toDate(SETTLEMENTDATE - INTERVAL 245 MINUTE)    AS trading_day,
    TOTALDEMAND                                     AS totaldemand,
    AVAILABLEGENERATION                             AS availablegeneration,
    AVAILABLELOAD                                   AS availableload,
    NETINTERCHANGE                                  AS netinterchange,
    UIGF                                            AS uigf,
    SEMISCHEDULE_CLEAREDMW                          AS semischedule_clearedmw,
    AVAILABLEGENERATION - TOTALDEMAND               AS margin,
    source,
    known_at
FROM
(
    SELECT
        *,
        row_number() OVER (
            PARTITION BY REGIONID, SETTLEMENTDATE
            ORDER BY multiIf(source = 'mmsdm', 1,
                             source = 'report_archive', 2, 3)
        ) AS pick
    FROM nemcast.dispatchregionsum
    WHERE INTERVENTION = 0
)
WHERE pick = 1;


-- The two forecast tables share a grain, so they join directly. Every interval
-- appears ~55 times, once per run — that repetition is the point, not redundancy.
CREATE OR REPLACE VIEW nemcast.v_forecast AS
SELECT
    p.REGIONID                                      AS regionid,
    p.DATETIME                                      AS datetime,
    toDate(p.DATETIME - INTERVAL 245 MINUTE)        AS trading_day,
    p.vintage                                       AS vintage,
    p.known_at                                      AS known_at,
    p.RUN_DATETIME                                  AS run_datetime,
    p.RRP                                           AS rrp,
    s.TOTALDEMAND                                   AS totaldemand,
    s.AVAILABLEGENERATION                           AS availablegeneration,
    s.AVAILABLELOAD                                 AS availableload,
    s.UIGF                                          AS uigf,
    s.SS_WIND_UIGF                                  AS wind,
    s.SS_SOLAR_UIGF                                 AS solar,
    s.AVAILABLEGENERATION - s.TOTALDEMAND           AS margin
FROM nemcast.predispatchprice AS p
INNER JOIN nemcast.predispatchregionsum AS s
       ON  p.REGIONID = s.REGIONID
       AND p.DATETIME = s.DATETIME
       AND p.vintage  = s.vintage
       AND p.source   = s.source
WHERE p.INTERVENTION = 0;


-- ============================================================ layer 2
-- Aggregation. Grain changes from interval to region-day.

-- The label. Days with fewer than 288 intervals are partial — the edges of the
-- series — and are dropped.
CREATE OR REPLACE VIEW nemcast.v_daily_outcome AS
SELECT
    regionid,
    trading_day,
    max(rrp)                AS actual_max_rrp,
    avg(rrp)                AS actual_mean_rrp,
    min(rrp)                AS actual_min_rrp,
    countIf(rrp > 300)      AS actual_n_over_300,
    countIf(rrp < 0)        AS actual_n_negative,
    toUInt8(max(rrp) > 300) AS spike,
    count()                 AS n_intervals
FROM nemcast.v_price_final
GROUP BY regionid, trading_day
HAVING n_intervals = 288;


-- Actuals aggregated to the day, for use as lags. Never as same-day features:
-- these are dispatch outputs, produced by the same solve that set the price.
CREATE OR REPLACE VIEW nemcast.v_daily_state AS
SELECT
    regionid,
    trading_day,
    max(totaldemand)        AS actual_max_demand,
    avg(totaldemand)        AS actual_mean_demand,
    min(margin)             AS actual_min_margin,
    avg(availableload)      AS actual_mean_load_bid
FROM nemcast.v_regionsum_final
GROUP BY regionid, trading_day;


-- Rolling persistence. Daily spike autocorrelation runs 0.28 to 0.42 at lag 1 —
-- heatwaves and windless periods last days — so this is real signal, and it is
-- known at the decision point.
--
-- The window excludes the current row: including today would leak the answer.
CREATE OR REPLACE VIEW nemcast.v_persistence AS
SELECT
    regionid,
    trading_day,
    sum(spike) OVER (
        PARTITION BY regionid ORDER BY trading_day
        ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING
    ) AS spike_count_7d,
    max(actual_max_rrp) OVER (
        PARTITION BY regionid ORDER BY trading_day
        ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING
    ) AS max_rrp_7d
FROM nemcast.v_daily_outcome;


-- Forecast features as at 13:00 on the day before. This is the first run that
-- reaches the whole of tomorrow's trading day: bids close at 12:30, so runs
-- before then have no view of it.
--
-- Per interval, take the most recent forecast published at or before the cutoff,
-- then aggregate across the day. Per-interval rather than per-run means a missing
-- or partial run still yields the best information genuinely available, instead
-- of losing the day entirely.
CREATE OR REPLACE VIEW nemcast.v_forecast_1300 AS
SELECT
    regionid,
    trading_day,
    max(rrp)                AS fc_max_rrp,
    avg(rrp)                AS fc_mean_rrp,
    quantile(0.9)(rrp)      AS fc_p90_rrp,
    countIf(rrp > 300)      AS fc_n_over_300,
    countIf(rrp < 0)        AS fc_n_negative,
    max(totaldemand)        AS fc_max_demand,
    avg(totaldemand)        AS fc_mean_demand,
    min(margin)             AS fc_min_margin,
    avg(margin)             AS fc_mean_margin,
    min(wind)               AS fc_min_wind,
    avg(wind)               AS fc_mean_wind,
    max(solar)              AS fc_max_solar,
    avg(availableload)      AS fc_mean_load_bid,
    count()                 AS fc_n_periods
FROM
(
    SELECT
        *,
        row_number() OVER (
            PARTITION BY regionid, datetime ORDER BY known_at DESC
        ) AS recency
    FROM nemcast.v_forecast
    -- coalesce because known_at is null for part of 2024: nemseer did not
    -- publish a write timestamp, and the fallback in backfill.py only fired
    -- when every value in a year was missing. A run label minus 30 minutes is
    -- the same estimate that branch would have produced.
    WHERE coalesce(known_at, run_datetime - INTERVAL 30 MINUTE)
          <= toDateTime64(toString(trading_day - 1) || ' 13:00:00', 0, 'UTC')
)
WHERE recency = 1
GROUP BY regionid, trading_day
HAVING fc_n_periods >= 40;


-- The decision point: 20:00 the evening before. Same logic, later cutoff.
-- The difference between the two is itself informative.
CREATE OR REPLACE VIEW nemcast.v_forecast_2000 AS
SELECT
    regionid,
    trading_day,
    max(rrp)                AS ev_max_rrp,
    avg(rrp)                AS ev_mean_rrp,
    quantile(0.9)(rrp)      AS ev_p90_rrp,
    countIf(rrp > 300)      AS ev_n_over_300,
    countIf(rrp < 0)        AS ev_n_negative,
    max(totaldemand)        AS ev_max_demand,
    avg(totaldemand)        AS ev_mean_demand,
    min(margin)             AS ev_min_margin,
    avg(margin)             AS ev_mean_margin,
    min(wind)               AS ev_min_wind,
    avg(wind)               AS ev_mean_wind,
    max(solar)              AS ev_max_solar,
    avg(availableload)      AS ev_mean_load_bid,
    count()                 AS ev_n_periods
FROM
(
    SELECT
        *,
        row_number() OVER (
            PARTITION BY regionid, datetime ORDER BY known_at DESC
        ) AS recency
    FROM nemcast.v_forecast
    WHERE coalesce(known_at, run_datetime - INTERVAL 30 MINUTE)
          <= toDateTime64(toString(trading_day - 1) || ' 20:00:00', 0, 'UTC')
)
WHERE recency = 1
GROUP BY regionid, trading_day
HAVING ev_n_periods >= 40;


-- ============================================================ layer 3
-- The modelling dataset. Materialised: queried constantly by the splits, the
-- model and the dashboard, and small enough that a full rebuild takes seconds.

DROP TABLE IF EXISTS nemcast.features;

CREATE TABLE nemcast.features
ENGINE = MergeTree
ORDER BY (regionid, trading_day)
AS
SELECT
    assumeNotNull(o.regionid)     AS regionid,
    assumeNotNull(o.trading_day)  AS trading_day,

    -- target
    o.spike              AS spike,

    -- outcome, for analysis. NOT features: drop before training.
    o.actual_max_rrp     AS actual_max_rrp,
    o.actual_mean_rrp    AS actual_mean_rrp,
    o.actual_n_over_300  AS actual_n_over_300,

    -- decision-point forecast, 20:00 the evening before
    e.ev_max_rrp,
    e.ev_mean_rrp,
    e.ev_p90_rrp,
    e.ev_n_over_300,
    e.ev_n_negative,
    e.ev_max_demand,
    e.ev_mean_demand,
    e.ev_min_margin,
    e.ev_mean_margin,
    e.ev_min_wind,
    e.ev_mean_wind,
    e.ev_max_solar,
    e.ev_mean_load_bid,

    -- earlier forecast, 13:00
    f.fc_max_rrp,
    f.fc_mean_rrp,
    f.fc_min_margin,
    f.fc_min_wind,
    f.fc_max_demand,

    -- how AEMO's view of the day moved over the afternoon. Only available
    -- because every forecast run is kept.
    e.ev_max_rrp    - f.fc_max_rrp      AS rev_max_rrp,
    e.ev_min_wind   - f.fc_min_wind     AS rev_min_wind,
    e.ev_max_demand - f.fc_max_demand   AS rev_max_demand,
    e.ev_min_margin - f.fc_min_margin   AS rev_min_margin,

    -- ratios, which generalise across the regime shift where levels do not.
    -- Tree models cannot extrapolate: fc_mean_load_bid quadrupled between 2023
    -- and 2026, so a model trained on the early period has no basis for the
    -- later one. A ratio means the same thing in both.
    e.ev_min_margin / nullIf(e.ev_max_demand, 0)    AS ev_margin_ratio,
    e.ev_mean_wind  / nullIf(e.ev_mean_demand, 0)   AS ev_wind_ratio,
    e.ev_mean_load_bid / nullIf(e.ev_mean_demand, 0) AS ev_load_bid_ratio,

    -- yesterday. The previous trading day ended at 04:00 this morning, so it is
    -- known at the 20:00 decision point.
    y.spike                 AS spike_yesterday,
    y.actual_max_rrp        AS max_rrp_yesterday,
    a.actual_max_demand     AS demand_yesterday,
    a.actual_min_margin     AS margin_yesterday,

    -- the last week
    p.spike_count_7d,
    p.max_rrp_7d,

    -- calendar
    toDayOfWeek(o.trading_day)                      AS dow,
    toMonth(o.trading_day)                          AS month,
    toUInt8(toDayOfWeek(o.trading_day) >= 6)        AS is_weekend,
    multiIf(toMonth(o.trading_day) IN (12, 1, 2), 'summer',
            toMonth(o.trading_day) IN (6, 7, 8),  'winter',
            'shoulder')                             AS season

FROM nemcast.v_daily_outcome AS o
INNER JOIN nemcast.v_forecast_2000 AS e
        ON o.regionid = e.regionid AND o.trading_day = e.trading_day
INNER JOIN nemcast.v_forecast_1300 AS f
        ON o.regionid = f.regionid AND o.trading_day = f.trading_day
LEFT  JOIN nemcast.v_daily_outcome AS y
        ON o.regionid = y.regionid AND y.trading_day = o.trading_day - 1
LEFT  JOIN nemcast.v_daily_state AS a
        ON o.regionid = a.regionid AND a.trading_day = o.trading_day - 1
LEFT  JOIN nemcast.v_persistence AS p
        ON o.regionid = p.regionid AND p.trading_day = o.trading_day;


-- ============================================================ checks

SELECT 'rows and spike rate by region' AS check;

SELECT
    regionid,
    count()                     AS days,
    min(trading_day)            AS first_day,
    max(trading_day)            AS last_day,
    round(avg(spike), 3)        AS spike_rate
FROM nemcast.features
GROUP BY regionid
ORDER BY regionid;


SELECT 'base rate by quarter — the structural break shows here' AS check;

SELECT
    toStartOfQuarter(trading_day)   AS quarter,
    round(avgIf(spike, regionid = 'NSW1'), 2) AS nsw,
    round(avgIf(spike, regionid = 'QLD1'), 2) AS qld,
    round(avgIf(spike, regionid = 'SA1'),  2) AS sa,
    round(avgIf(spike, regionid = 'TAS1'), 2) AS tas,
    round(avgIf(spike, regionid = 'VIC1'), 2) AS vic
FROM nemcast.features
GROUP BY quarter
ORDER BY quarter;


SELECT 'AEMO benchmark: calling a spike when the 20:00 forecast exceeds 300' AS check;

SELECT
    period,
    tp, fp, fn,
    round(tp / nullIf(tp + fp, 0), 3) AS precision,
    round(tp / nullIf(tp + fn, 0), 3) AS recall
FROM
(
    SELECT
        multiIf(trading_day < '2025-01-01', '2023-24',
                trading_day < '2026-01-01', '2025', '2026') AS period,
        countIf(spike = 1 AND ev_max_rrp > 300)  AS tp,
        countIf(spike = 0 AND ev_max_rrp > 300)  AS fp,
        countIf(spike = 1 AND ev_max_rrp <= 300) AS fn
    FROM nemcast.features
    GROUP BY period
)
ORDER BY period;


SELECT 'coverage: expected days vs actual' AS check;

SELECT
    toYear(trading_day)         AS year,
    count()                     AS region_days,
    countDistinct(trading_day)  AS days,
    round(count() / 5, 0)       AS days_per_region
FROM nemcast.features
GROUP BY year
ORDER BY year;


SELECT 'null features — a null here means a lag or join found nothing' AS check;

SELECT
    countIf(spike_yesterday IS NULL)    AS no_yesterday,
    countIf(spike_count_7d IS NULL)     AS no_7d,
    countIf(fc_max_rrp IS NULL)         AS no_1300_forecast,
    countIf(ev_max_rrp IS NULL)         AS no_2000_forecast,
    count()                             AS total
FROM nemcast.features;


-- The point-in-time guarantee lives in the two forecast views: a row can only
-- contribute if its known_at precedes the decision point. There is nothing to
-- check here that the views do not already enforce.
--
-- A naive check comparing known_at against the start of the target day would
-- flag ~10M rows, but those are legitimate: pre-dispatch forecasts the current
-- day as well as the next one, and a run at 20:00 forecasting 22:00 the same
-- evening is not leakage — it is simply never selected by the views.
