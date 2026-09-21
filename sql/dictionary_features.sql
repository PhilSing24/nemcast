-- NEMCast — data dictionary for nemcast.features
--
--     ~/clickhouse client --host ::1 --multiquery < sql/dictionary_features.sql
--
-- Adds one row per column of the modelling table to nemcast.data_dictionary,
-- alongside the raw-table entries already there.
--
-- The `timing` column is the whole point:
--
--   target    what the model predicts
--   outcome   describes the day being predicted — NOT a feature, drop before training
--   feature   knowable at 20:00 the evening before
--   key       identifies the row

DELETE FROM nemcast.data_dictionary WHERE table_name = 'features';

INSERT INTO nemcast.data_dictionary VALUES

-- ---------------------------------------------------------------- keys
('features', 'regionid',    'key', '',     'NEM region. Include as a categorical feature: base rates range from 17% in VIC1 to 40% in SA1, and the model needs to condition on that.'),
('features', 'trading_day', 'key', 'date', 'The day being predicted, running 04:05 to 04:00. Use for splitting and joining; never as a feature — a datetime column lets the model memorise specific dates.'),

-- ---------------------------------------------------------------- target
('features', 'spike', 'target', '', 'THE TARGET. Did any 5-minute interval of this trading day exceed $300? Decided at 20:00 the previous evening, so the horizon is 8 to 32 hours. $300 is the strike on a standard ASX Energy cap contract, not an arbitrary round number.'),

-- ---------------------------------------------------------------- outcome
('features', 'actual_max_rrp',    'outcome', '$/MWh', 'Highest price of the day. The target is derived from this, so it cannot be a feature.'),
('features', 'actual_mean_rrp',   'outcome', '$/MWh', 'Mean price across the day. For analysis only.'),
('features', 'actual_n_over_300', 'outcome', 'count', 'How many of the 288 intervals exceeded $300. A day with 268 is a different event from a day with 1, and worth knowing when reading results.'),

-- ---------------------------------------------------------------- 20:00 forecast
('features', 'ev_max_rrp',        'feature', '$/MWh', 'AEMOs highest forecast price for the day, as at 20:00. THE BENCHMARK: calling a spike when this exceeds $300 gives precision 0.62 in 2023-24, 0.49 in 2025, 0.31 in 2026. Its mean of $1,383 against an actual mean of $790 shows the systematic over-forecast.'),
('features', 'ev_mean_rrp',       'feature', '$/MWh', 'Mean forecast price across the day.'),
('features', 'ev_p90_rrp',        'feature', '$/MWh', '90th percentile of the forecast. Less sensitive than the max to one alarming period.'),
('features', 'ev_n_over_300',     'feature', 'count', 'How many of the days 48 half-hourly periods are forecast above $300. Median 0, but reaches 48 — a whole day expected above the cap.'),
('features', 'ev_n_negative',     'feature', 'count', 'Periods forecast below zero. High values mean abundant renewables, which usually means no spike.'),
('features', 'ev_max_demand',     'feature', 'MW',    'Forecast peak demand. Usually the strongest single feature in price forecasting.'),
('features', 'ev_mean_demand',    'feature', 'MW',    'Forecast mean demand.'),
('features', 'ev_min_margin',     'feature', 'MW',    'Tightest forecast margin — available generation minus demand — across the day. Negative means the region expects to rely on imports. But margin alone is a poor predictor: 500 MW of headroom priced at $12,000 does not protect you.'),
('features', 'ev_mean_margin',    'feature', 'MW',    'Mean forecast margin.'),
('features', 'ev_min_wind',       'feature', 'MW',    'Lowest forecast wind of the day. Wind is where nearly all renewable forecast error lives, and a collapse is the classic spike precursor.'),
('features', 'ev_mean_wind',      'feature', 'MW',    'Mean forecast wind.'),
('features', 'ev_max_solar',      'feature', 'MW',    'Peak forecast solar. Predictable — astronomical shape, cloud the only real uncertainty. Its disappearance in the evening is what creates the peak that spikes come from.'),
('features', 'ev_mean_load_bid',  'feature', 'MW',    'Mean forecast available load — batteries and pumped hydro bidding to charge. Quadrupled between 2023 and 2026 and is the leading explanation for the collapse in spike rates. Also the feature that breaks extrapolation: test values sit far outside the training range.'),

-- ---------------------------------------------------------------- 13:00 forecast
('features', 'fc_max_rrp',     'feature', '$/MWh', 'Highest forecast price as at 13:00, the first run reaching tomorrow. Mean $2,206 against $1,383 at 20:00 — the earlier run is markedly more alarmist, and the gap between them is informative.'),
('features', 'fc_mean_rrp',    'feature', '$/MWh', 'Mean forecast price at 13:00.'),
('features', 'fc_min_margin',  'feature', 'MW',    'Tightest forecast margin at 13:00.'),
('features', 'fc_min_wind',    'feature', 'MW',    'Lowest forecast wind at 13:00.'),
('features', 'fc_max_demand',  'feature', 'MW',    'Forecast peak demand at 13:00.'),

-- ---------------------------------------------------------------- revisions
('features', 'rev_max_rrp',     'feature', '$/MWh', 'How AEMOs peak price forecast moved between 13:00 and 20:00. Mean -$824, median -$16: the afternoon almost always walks the forecast DOWN, occasionally by more than $22,000. A forecast that instead moves up is a genuine warning. Only computable because every vintage is kept.'),
('features', 'rev_min_wind',    'feature', 'MW',    'Revision to the minimum wind forecast. Wind revised down into an already tight evening is the strongest physical spike signal available.'),
('features', 'rev_max_demand',  'feature', 'MW',    'Revision to peak demand.'),
('features', 'rev_min_margin',  'feature', 'MW',    'Revision to the tightest margin.'),

-- ---------------------------------------------------------------- ratios
('features', 'ev_margin_ratio',   'feature', 'ratio', 'Tightest margin divided by peak demand. Dimensionless, so it means the same thing in 2023 and 2026 — unlike the raw megawatt level, which drifted. Tree models cannot extrapolate beyond their training range, so ratios are what survive a regime shift.'),
('features', 'ev_wind_ratio',     'feature', 'ratio', 'Mean wind divided by mean demand. How much of the load renewables are expected to cover.'),
('features', 'ev_load_bid_ratio', 'feature', 'ratio', 'Available load divided by demand. Storage capacity relative to the market it sits in — the scale-free version of the battery build-out.'),

-- ---------------------------------------------------------------- lags
('features', 'spike_yesterday',    'feature', '',      'Did the previous trading day spike? It ended at 04:00 this morning, so it is known at the 20:00 decision point. Daily spike autocorrelation runs 0.28 to 0.42 at lag 1 — heatwaves and windless periods last days.'),
('features', 'max_rrp_yesterday',  'feature', '$/MWh', 'Yesterdays highest price. The continuous version of the above.'),
('features', 'demand_yesterday',   'feature', 'MW',    'Yesterdays peak demand. Actual, not forecast — a dispatch output, safe only because it is lagged.'),
('features', 'margin_yesterday',   'feature', 'MW',    'Yesterdays tightest margin.'),
('features', 'spike_count_7d',     'feature', 'count', 'Spike days in the previous seven, excluding today. Captures a run of hot or windless weather rather than a single day.'),
('features', 'max_rrp_7d',         'feature', '$/MWh', 'Highest price in the previous seven days, excluding today.'),

-- ---------------------------------------------------------------- calendar
('features', 'dow',        'feature', '', 'Day of week, 1 = Monday. Demand shape differs on weekends.'),
('features', 'month',      'feature', '', 'Month. Adjacent in reality but far apart numerically — December and January are neighbours. Trees handle this by splitting; a linear model would not.'),
('features', 'is_weekend', 'feature', '', 'Saturday or Sunday. Lower commercial and industrial load.'),
('features', 'season',     'feature', '', 'summer (Dec-Feb), winter (Jun-Aug), shoulder. Summer evenings drive spikes in the mainland regions.');


-- ---------------------------------------------------------------- read it back

SELECT
    d.column_name   AS feature,
    d.timing        AS timing,
    d.unit          AS unit,
    d.description   AS description
FROM nemcast.data_dictionary AS d
WHERE d.table_name = 'features'
ORDER BY
    multiIf(d.timing = 'key', 1, d.timing = 'target', 2,
            d.timing = 'outcome', 3, 4),
    d.column_name;
