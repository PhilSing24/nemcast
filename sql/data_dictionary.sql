-- NEMCast — data dictionary
--
-- One row per column across the four warehouse tables. Lives in the database
-- rather than a document so it can be joined to system.columns and cannot
-- drift out of sync with the schema.
--
-- The `timing` column is the one that matters for modelling. Every feature must
-- be knowable before the interval it predicts:
--
--   key       identifies the row
--   input     existed before NEMDE ran — safe to use as a feature
--   output    produced by the same solve that set the price — leakage unless lagged
--   forecast  published ahead of the interval — safe by construction
--   meta      provenance, not market data

DROP TABLE IF EXISTS nemcast.data_dictionary;

CREATE TABLE nemcast.data_dictionary
(
    table_name  LowCardinality(String),
    column_name String,
    timing      LowCardinality(String),
    unit        LowCardinality(String),
    description String
)
ENGINE = MergeTree
ORDER BY (table_name, column_name);


-- ------------------------------------------------------------ shared columns

INSERT INTO nemcast.data_dictionary VALUES
('*', 'REGIONID',      'key',  '',         'NEM region: NSW1, QLD1, VIC1, SA1, TAS1. NSW1 includes the ACT; WA and NT are not in the NEM.'),
('*', 'INTERVENTION',  'key',  '',         '0 = the pricing run, which settles. 1 = the physical run, published only when AEMO directed units outside normal dispatch. Filter to 0 unless you specifically want what physically happened.'),
('*', 'source',        'meta', '',         'Which feed this row came from: mmsdm (monthly archive, final), report_archive (daily bundles), report_current (live feed). The same interval can appear from all three.'),
('*', 'known_at',      'meta', 'timestamp','When this value became available. Observed from the file timestamp for report rows; estimated for MMSDM rows. This is the column that makes point-in-time queries possible.'),
('*', 'known_at_kind', 'meta', '',         'Whether known_at is estimated or taken from a file timestamp. Without this you cannot tell how much to trust it.'),
('*', 'ingested_at',   'meta', 'timestamp','When this row was written to the warehouse. Always real. Two rows with the same known_at but different ingested_at indicates a revision.'),
('*', 'source_file',   'meta', '',         'The file this row came from. Used by the daily job to skip what it already has.'),
('*', 'vintage',       'key',  '',         'Unified forecast run identifier, coalesced from RUN_DATETIME (MMSDM) or PREDISPATCHSEQNO (report files). Forecast tables only. Dropping it from a key collapses 55 forecasts into one.');


-- ------------------------------------------------------------ dispatchprice
-- The actual cleared price. One row per 5-minute interval per region.
-- This table holds the target variable.

INSERT INTO nemcast.data_dictionary VALUES
('dispatchprice', 'SETTLEMENTDATE',      'key',    'timestamp', 'Interval END. The label 00:05 covers 00:00 to 00:05. Fixed AEST, no daylight saving. Trading day runs 04:00 to 04:00, so subtract 245 minutes to get the trading date.'),
('dispatchprice', 'RRP',                 'output', '$/MWh',     'Regional Reference Price — THE TARGET. The offer price of the marginal unit: the last and most expensive megawatt NEMDE had to dispatch. Everyone dispatched is paid this, whatever they bid. Floor -$1,000, cap ~$20,300 rising annually.'),
('dispatchprice', 'PRICE_STATUS',        'output', '',          'FIRM means settled and unrevisable. Prices can be adjusted until the second business day of the following month.'),
('dispatchprice', 'APCFLAG',             'output', '',          'Administered price cap applied. Set when the cumulative price threshold trips — a rolling sum of recent prices exceeding a limit — as happened market-wide in June 2022. Not a market outcome; exclude from training.'),
('dispatchprice', 'MARKETSUSPENDEDFLAG', 'output', '',          'Market suspended. AEMO dispatched administratively rather than by the merit order. Exclude from training.'),
('dispatchprice', 'RAISE6SECRRP',        'output', '$/MWh',     'FCAS: price for capacity able to raise output within 6 seconds after a contingency.'),
('dispatchprice', 'RAISE60SECRRP',       'output', '$/MWh',     'FCAS: raise response sustained to 60 seconds.'),
('dispatchprice', 'RAISE5MINRRP',        'output', '$/MWh',     'FCAS: raise response sustained to 5 minutes, until normal dispatch catches up.'),
('dispatchprice', 'RAISEREGRRP',         'output', '$/MWh',     'FCAS: continuous regulation upward, following AEMO signal second by second. Batteries dominate this market.'),
('dispatchprice', 'RAISE1SECRRP',        'output', '$/MWh',     'FCAS: very fast raise, introduced for inverter-based resources.'),
('dispatchprice', 'LOWER6SECRRP',        'output', '$/MWh',     'FCAS: lower output within 6 seconds. Needed when frequency rises — oversupply.'),
('dispatchprice', 'LOWER60SECRRP',       'output', '$/MWh',     'FCAS: lower response sustained to 60 seconds.'),
('dispatchprice', 'LOWER5MINRRP',        'output', '$/MWh',     'FCAS: lower response sustained to 5 minutes.'),
('dispatchprice', 'LOWERREGRRP',         'output', '$/MWh',     'FCAS: continuous regulation downward.'),
('dispatchprice', 'LOWER1SECRRP',        'output', '$/MWh',     'FCAS: very fast lower.');


-- ------------------------------------------------------------ dispatchregionsum
-- The physical state behind each price. Mostly dispatch OUTPUTS — produced by
-- the same NEMDE solve that set the price, so leakage if used at the same
-- timestamp. Their lags are legitimate and useful.

INSERT INTO nemcast.data_dictionary VALUES
('dispatchregionsum', 'SETTLEMENTDATE',              'key',    'timestamp', 'Interval end, as in dispatchprice.'),
('dispatchregionsum', 'TOTALDEMAND',                 'output', 'MW',        'OPERATIONAL demand — what the grid had to serve, net of rooftop solar. AEMO cannot see behind-the-meter generation, so on a mild sunny Sunday SA has fallen to 65 MW while households ran on their own panels.'),
('dispatchregionsum', 'AVAILABLEGENERATION',         'input',  'MW',        'Sum of every generation bid in the region across all price bands. Known before dispatch. Says nothing about price: 500 MW of headroom priced at $12,000 does not protect you from a spike.'),
('dispatchregionsum', 'AVAILABLELOAD',               'input',  'MW',        'Sum of load bids — batteries offering to charge, pumped hydro pumping. Flexible demand that only appears when prices are low. This is the floor under negative prices, and it quadrupled between 2023 and 2026.'),
('dispatchregionsum', 'DEMANDFORECAST',              'output', 'MW',        'A DELTA, not a level: the expected change in demand over the interval, typically plus or minus 13 MW. Not the demand forecast you want — that is TOTALDEMAND in the pre-dispatch tables.'),
('dispatchregionsum', 'DISPATCHABLEGENERATION',      'output', 'MW',        'What NEMDE instructed scheduled units to produce.'),
('dispatchregionsum', 'DISPATCHABLELOAD',            'output', 'MW',        'What NEMDE instructed scheduled loads to consume — batteries actually charging.'),
('dispatchregionsum', 'NETINTERCHANGE',              'output', 'MW',        'Net interconnector flow. NEGATIVE means importing. Essential context for margin: a region can have negative local margin and still be fine if imports cover it. When this pins at a limit, regional prices separate.'),
('dispatchregionsum', 'EXCESSGENERATION',            'output', 'MW',        'Surplus with nowhere to go — interconnectors already at their export limit. Usually zero; non-zero marks genuine physical oversupply.'),
('dispatchregionsum', 'INITIALSUPPLY',               'input',  'MW',        'Generation at the START of the interval, inherited from the previous one. Safe to use. CLEAREDSUPPLY minus this is the ramp, a direct measure of how hard the system is being asked to move.'),
('dispatchregionsum', 'CLEAREDSUPPLY',               'output', 'MW',        'Generation at the END of the interval — the solve result. Plants cannot jump: ramp rate limits mean a cheap unit sitting low may be unable to reach the required level in five minutes, forcing NEMDE to dispatch something dearer and closer.'),
('dispatchregionsum', 'UIGF',                        'input',  'MW',        'Unconstrained Intermittent Generation Forecast — what wind and solar COULD produce if nothing curtailed them. AEMOs own forecast, published ahead, and it caps how much a semi-scheduled unit can be dispatched to. The most useful column in this table.'),
('dispatchregionsum', 'SEMISCHEDULE_CLEAREDMW',      'output', 'MW',        'What semi-scheduled wind and solar were actually told to produce. UIGF minus this is CURTAILMENT — and curtailment is caused by the price, not predictive of it. Using the gap as a feature looks brilliant in backtest and is worthless live.'),
('dispatchregionsum', 'SEMISCHEDULE_COMPLIANCEMW',   'output', 'MW',        'How closely semi-scheduled units followed their dispatch instruction.'),
('dispatchregionsum', 'TOTALINTERMITTENTGENERATION', 'output', 'MW',        'Non-scheduled intermittent generation — small units below the registration threshold that generate without bidding. A different population from SEMISCHEDULE_CLEAREDMW, which is why the two can differ wildly.');


-- ------------------------------------------------------------ predispatchprice
-- AEMOs forecast price, one row per interval PER RUN. Published before the
-- interval, so safe by construction. Also the benchmark NEMCast must beat.

INSERT INTO nemcast.data_dictionary VALUES
('predispatchprice', 'DATETIME',         'key',      'timestamp', 'The interval being FORECAST. 30-minute resolution, not 5-minute.'),
('predispatchprice', 'RUN_DATETIME',     'key',      'timestamp', 'Run label — the first interval the run covers, NOT when it executed. A run labelled 13:00 was written around 12:30. MMSDM rows only.'),
('predispatchprice', 'PREDISPATCHSEQNO', 'key',      '',          'Run identifier, format YYYYMMDDRR. Report-file rows only; MMSDM uses RUN_DATETIME instead. The vintage column coalesces the two.'),
('predispatchprice', 'LASTCHANGED',      'meta',     'timestamp', 'When the record was written. Closer to true publication time than the run label.'),
('predispatchprice', 'RRP',              'forecast', '$/MWh',     'AEMOs forecast price. Both a strong feature and THE BENCHMARK: on 2026 data, calling a spike when this exceeds $300 gives 0.236 precision at 0.784 recall. Recall held steady from 2023 while precision collapsed from 0.58 — pre-dispatch assumes the current bid stack holds and does not model batteries rebidding into the evening peak.'),
('predispatchprice', 'EEP',              'forecast', '$/MWh',     'Excess energy price.'),
('predispatchprice', 'RRP1',             'forecast', '$/MWh',     'Price under demand scenario 1. RRP1 to RRP8 are AEMOs sensitivities — the same interval repriced under alternative demand assumptions. The SPREAD across them measures how fragile the interval is: $200 under every scenario is robust, $150 to $8,000 is not. Populated only in some runs.'),
('predispatchprice', 'RRP2',             'forecast', '$/MWh',     'Price under demand scenario 2.'),
('predispatchprice', 'RRP3',             'forecast', '$/MWh',     'Price under demand scenario 3.'),
('predispatchprice', 'RRP4',             'forecast', '$/MWh',     'Price under demand scenario 4.'),
('predispatchprice', 'RRP5',             'forecast', '$/MWh',     'Price under demand scenario 5.'),
('predispatchprice', 'RRP6',             'forecast', '$/MWh',     'Price under demand scenario 6.'),
('predispatchprice', 'RRP7',             'forecast', '$/MWh',     'Price under demand scenario 7.'),
('predispatchprice', 'RRP8',             'forecast', '$/MWh',     'Price under demand scenario 8.');


-- ------------------------------------------------------------ predispatchregionsum
-- Forecast demand and renewables, same vintage structure. After the leakage
-- audit this is where nearly every legitimate feature comes from.

INSERT INTO nemcast.data_dictionary VALUES
('predispatchregionsum', 'DATETIME',              'key',      'timestamp', 'The interval being forecast.'),
('predispatchregionsum', 'RUN_DATETIME',          'key',      'timestamp', 'Run label. MMSDM rows only.'),
('predispatchregionsum', 'PREDISPATCHSEQNO',      'key',      '',          'Run identifier. Report-file rows only.'),
('predispatchregionsum', 'LASTCHANGED',           'meta',     'timestamp', 'When the record was written.'),
('predispatchregionsum', 'TOTALDEMAND',           'forecast', 'MW',        'FORECAST demand as a LEVEL — the real demand forecast, unlike the delta in dispatchregionsum.DEMANDFORECAST. Usually the strongest single feature in electricity price forecasting.'),
('predispatchregionsum', 'AVAILABLEGENERATION',   'forecast', 'MW',        'Forecast generation availability from bids.'),
('predispatchregionsum', 'AVAILABLELOAD',         'forecast', 'MW',        'Forecast load bids. Quadrupled between 2023 and 2026 as battery capacity grew — the single feature that most separates the two periods, and the leading explanation for the collapse in spike rates.'),
('predispatchregionsum', 'DEMANDFORECAST',        'forecast', 'MW',        'Expected change in demand over the interval.'),
('predispatchregionsum', 'NETINTERCHANGE',        'forecast', 'MW',        'Forecast interconnector flow. Negative means importing.'),
('predispatchregionsum', 'EXCESSGENERATION',      'forecast', 'MW',        'Forecast surplus with nowhere to go.'),
('predispatchregionsum', 'INITIALSUPPLY',         'forecast', 'MW',        'Forecast generation at interval start.'),
('predispatchregionsum', 'CLEAREDSUPPLY',         'forecast', 'MW',        'Forecast generation at interval end.'),
('predispatchregionsum', 'UIGF',                  'forecast', 'MW',        'Combined wind and solar forecast. Prefer the split below: at 18:30 in February solar is a known zero while wind is the entire uncertainty, and averaging them destroys that.'),
('predispatchregionsum', 'SS_SOLAR_UIGF',         'forecast', 'MW',        'Solar forecast alone. Predictable — the shape is astronomical and cloud is the only real uncertainty. Zero at night.'),
('predispatchregionsum', 'SS_WIND_UIGF',          'forecast', 'MW',        'Wind forecast alone. Volatile, can collapse at any hour. Nearly all renewable forecast error lives here, and it is the largest source of price surprise.'),
('predispatchregionsum', 'SEMISCHEDULE_CLEAREDMW','forecast', 'MW',        'Forecast semi-scheduled dispatch.');


-- ------------------------------------------------------------ usage

-- Every column of one table, with its description and actual type.
-- SELECT c.name, c.type, d.timing, d.unit, d.description
-- FROM system.columns AS c
-- LEFT JOIN nemcast.data_dictionary AS d
--        ON d.column_name = c.name
--       AND (d.table_name = c.table OR d.table_name = '*')
-- WHERE c.database = 'nemcast' AND c.table = 'dispatchregionsum'
-- ORDER BY c.position;

-- Which columns are safe to use as features?
-- SELECT table_name, column_name, description
-- FROM nemcast.data_dictionary
-- WHERE timing IN ('input', 'forecast')
-- ORDER BY table_name, column_name;

-- Columns in the schema with no description yet.
-- SELECT DISTINCT c.table, c.name
-- FROM system.columns AS c
-- LEFT JOIN nemcast.data_dictionary AS d
--        ON d.column_name = c.name
--       AND (d.table_name = c.table OR d.table_name = '*')
-- WHERE c.database = 'nemcast' AND d.column_name = ''
-- ORDER BY c.table, c.name;
