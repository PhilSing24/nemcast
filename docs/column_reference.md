# NEMCast — column reference

All example values below come from one worked interval: **SA1, Thursday 2024-02-15 18:30**
(covering 18:25–18:30). Warm evening, wind dropping off, sun down.

## The worked example

**Generation bids for the interval:**

| Unit | Qty | Offer |
|---|---|---|
| Wind farms | 340 MW | −$55 |
| Solar | 0 MW | — (sun down) |
| Torrens Island B (CCGT) | 380 MW | $72 |
| Barker Inlet (recip gas) | 210 MW | $145 |
| Osborne (gas) | 180 MW | $240 |
| Battery (discharging) | 150 MW | $310 |
| Peaker A (OCGT) | 160 MW | $580 |
| Peaker B (OCGT) | 130 MW | $4,200 |

Total offered: **1,550 MW** → `AVAILABLEGENERATION`

**Load bid:** Battery C offers to charge 90 MW if the price is under $40 → `AVAILABLELOAD`

**Conditions:** demand 1,780 MW, wind forecast 340 MW, importing 420 MW from Victoria.

**The solve.** Local supply needed after imports: 1,780 − 420 = 1,360 MW. Stack cheapest first:

| Cumulative | Unit | Offer |
|---|---|---|
| 340 | Wind | −$55 |
| 720 | Torrens | $72 |
| 930 | Barker | $145 |
| 1,110 | Osborne | $240 |
| **1,360** | Battery, 130 of 150 MW | **$310** |

The battery is marginal, so **RRP = $310**. Everyone dispatched is paid $310 — including the
wind farms that bid −$55. Both peakers sit unused. Battery C doesn't charge; $310 is far above
its $40 limit.

**What to notice:** had wind come in 130 MW lower, Peaker B would have been marginal and the
price would have been **$4,200** instead of $310. One unit, a modest forecast miss, a
thirteen-fold price difference. That is the fat tail, mechanically.

---

**The test for every column: did this exist before NEMDE ran?**
If yes → usable feature. If no → it's an output of the same solve that set the price, so it's
leakage unless lagged.

---

## DISPATCHPRICE

| Column | Example | Meaning | Timing |
|---|---|---|---|
| `SETTLEMENTDATE` | 2024-02-15 18:30 | Interval **ending**. Fixed AEST, no DST. | key |
| `REGIONID` | SA1 | One of NSW1, QLD1, VIC1, SA1, TAS1 | key |
| `RRP` | 310.00 | **The target.** Offer price of the marginal unit. Everyone dispatched is paid this. Floor −$1,000, cap ~$20,300. | output |
| `PRICE_STATUS` | FIRM | FIRM = settled, cannot be revised | output |
| `INTERVENTION` | 0 | 0 = pricing run (settles). 1 = physical run when AEMO directed units. Both rows exist on intervened intervals. | key |
| `RAISE6SECRRP` etc | 0.39 | Eight FCAS prices — grid stability services, co-optimised in the same solve. Not needed for energy price modelling. | output |

---

## DISPATCHREGIONSUM

### Known before dispatch — usable

| Column | Example | Meaning |
|---|---|---|
| `AVAILABLEGENERATION` | 1550.00 | Sum of all generation **bids** in the region. The capacity ceiling. Says nothing about price — 500 MW of headroom at $12,000 doesn't protect you. |
| `AVAILABLELOAD` | 90.00 | Sum of **load** bids — batteries charging, pumped hydro. Flexible demand that appears when prices are low. This is the floor under negative prices. |
| `UIGF` | 340.00 | **Unconstrained Intermittent Generation Forecast.** AEMO's forecast of what wind+solar *could* produce. Published ahead. The single most useful column here. |
| `INITIALSUPPLY` | 1762.00 | Actual generation at the interval's **start**. Inherited state, safe to use. |
| `DEMANDFORECAST` | 14.20 | A **delta** (±13 MW typical), not a level. Expected change in demand over the interval — a ramp signal. Timing not fully documented; treat with care. |

### Dispatch outputs — leakage if used at the same timestamp

| Column | Example | Meaning |
|---|---|---|
| `TOTALDEMAND` | 1780.00 | Demand actually served. **Operational demand** — net of rooftop solar, which AEMO cannot see. Excellent as a lag. |
| `CLEAREDSUPPLY` | 1780.00 | Generation at the interval's **end**. The solve's target. `CLEAREDSUPPLY − INITIALSUPPLY` = the ramp. |
| `DISPATCHABLEGENERATION` | 1020.00 | MW of scheduled units NEMDE instructed |
| `DISPATCHABLELOAD` | 0.00 | MW of load bids dispatched (battery didn't charge — price too high) |
| `NETINTERCHANGE` | −420.00 | Interconnector flow. **Negative = importing.** Essential context for margin: local generation alone was short 230 MW here; imports covered it. |
| `SEMISCHEDULE_CLEAREDMW` | 340.00 | What semi-scheduled wind/solar was told to produce. **`UIGF − this` = curtailment** — caused by price, not predictive of it. |
| `TOTALINTERMITTENTGENERATION` | 12.00 | Non-scheduled intermittent (small units that don't bid). Different population from semi-scheduled — verify the definition before using. |
| `EXCESSGENERATION` | 0.00 | Surplus with nowhere to go — interconnectors already at export limit. Usually zero; non-zero marks extreme oversupply. |
| `*LOCALDISPATCH` (×8) | — | FCAS procured locally. Ignore for energy price modelling. |
| `DISPATCHINTERVAL` | 20240215174 | `YYYYMMDDPPP`. Period 1–288 of the **trading day**, which starts 04:05. Period 240 is midnight, not period 1. |

---

## PREDISPATCH tables — the forecast side

Same columns, but with **two time axes**, which is what makes them usable as features:

| Column | Meaning |
|---|---|
| `PREDISPATCH_RUN_DATETIME` | When the forecast was **made**. Note: run identity, not publication time — files land a few minutes later. |
| `DATETIME` | The interval being **forecast** |
| `PREDISPATCHSEQNO` | Run identifier |

Each target interval appears ~40 times, once per run, out to ~40 hours ahead.
Resolution is **30-minute**, not 5-minute.

`PREDISPATCHREGIONSUM` splits the renewable forecast, which the dispatch table doesn't:

| Column | Why it matters |
|---|---|
| `SS_SOLAR_UIGF` | Predictable — astronomical shape, cloud is the only uncertainty. Zero at night. |
| `SS_WIND_UIGF` | Volatile — can collapse at any hour. Nearly all forecast error lives here. |

---

## The two facts that shape the model

**Price = the marginal offer.** Demand, weather and outages matter only because they
determine how far up the bid stack NEMDE has to reach. A modest wind shortfall can move the
marginal unit from a $310 battery to a $4,200 peaker.

**15.4% of intervals are negative.** Wind and solar bid below zero because certificates and
PPAs pay per MWh generated — being curtailed earns nothing, so paying $50 to earn $100 is
rational.
