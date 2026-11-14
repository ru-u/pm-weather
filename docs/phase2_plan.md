# Phase 2 plan: do Kalshi snow prices beat free NBM guidance?

Status: **plan accepted, pre-registration frozen 2026-11-14. Steps 1 to 6 not
yet run** (the hourly candles for step 1 are already cached by
`scripts/prereg_design.py`). The decision rule is in `docs/preregistration.md`.

## The question

At matched timestamps, is the Kalshi snow market's implied probability more
accurate, as accurate, or less accurate than a probability built only from free
NBM station guidance (plus climatology where NBM does not reach)?

## What day one changed about the plan

1. **The Kalshi sample is small.** About 62 settled city-months and 6 storm
   windows, with heavy clustering. Phase 2 can detect a large gap between market
   and NBM, not a small one. The rule therefore has an explicit "inconclusive"
   outcome, and Phase 2 gets a second, large-sample part (2B) that does not
   depend on Kalshi at all.
2. **NBM does not cover a month.** NBP percentiles run about 9.5 days. A monthly
   contract's probability needs three pieces: snow already observed, NBM for the
   forecast window, and climatology for the days beyond it. That composition is
   a model with choices, and the choices are fixed in the pre-registration.
3. **Period mismatch.** NBP's 24-hour periods end at 12Z; a climate day at an
   Eastern station runs 05Z to 05Z. Storm-window contracts need a fixed rule for
   this.
4. **Last winter's NBM was V4.3**, not V5. Results describe the market against
   V4.3 guidance. Whether V5.x is better is a separate question for 2B.
5. **Settlement source hierarchy changed** this season (The Weather Company
   first). Historical replication uses NWS CLI, which is what last season's rules
   cited.

## Part 2A: market versus NBM on settled Kalshi markets

### Step 1. Market data (laptop, about a day)

- Inventory every settled snow series: `KX*SNOWM` (11 cities), `KXSNOWNYM` and
  `SNOWNYM`, `KXSNOWSTORM`, plus `KXSNOWNYC`, `KXSNOWNY`, `SNOW`, Christmas and
  Thanksgiving markets. Ski resorts are excluded (different source, no NBM station).
- For each market pull metadata with rule text, hourly candlesticks (trade OHLC,
  YES bid and ask OHLC, volume, open interest) and the full trade tape.
- Store raw responses under `data/raw/kalshi/`, parsed tables under `data/interim/`.
- Output: one row per (market, hour) with bid, ask, mid, volume.

### Step 2. Outcomes and settlement replication (DONE 2026-11-14: 389 of 389 outside the excluded Chicago event)

- Extend `pmw settle-check` to storm windows (parse window dates and station
  from rule text) and to the older NYC series.
- Close the 34 undecided markets with a second source for days where IEM has no
  CLI snow value. Candidates, in order: IEM's raw CLI text archive (earlier or
  later issuances), the monthly CLM report, NOWData.
- Explain or exclude Chicago December 2025.
- **Gate:** every market in the analysis sample reproduces Kalshi's result. Any
  market that cannot be reproduced is listed with a reason and excluded before
  step 4, not after.

### Step 3. NBM station forecasts, issue-time aware (two to three days)

- Download NBP and NBS text bulletins for the settlement stations, for every
  cycle in each market's life, from `noaa-nbm-grib2-pds`. Keep only our station
  blocks (a few KB per cycle), raw, under `data/raw/nbm/`.
- Parser with tests on saved real blocks. Record the NBM version from each header.
- **Availability time, not issue time.** A cycle is usable at `available_at`,
  taken from the S3 object's LastModified where that is trustworthy, otherwise
  issue time plus a conservative fixed lag. The join rule is
  `available_at < market_ts`, enforced by an automated leakage test.
- Build per-station snowfall climatology from CLI/GHCN daily data
  (1991 to 2020 plus recent years), by day of year, as a distribution, not a mean.

### Step 4. Turn NBM into a probability for each strike

For a contract "total snowfall over days D1..Dn is above K", at decision time t:

    Total = Observed(days fully reported before t)
          + Forecast(days inside the NBM window)
          + Climatology(days beyond the NBM window)

- **Observed:** CLI values for days whose report was available before t.
- **Forecast:** a distribution for each 24-hour period from the five NBP
  percentiles (10/25/50/75/90), with a point mass at zero where the percentiles
  are zero and a fitted right tail above the 90th. Periods are summed by Monte
  Carlo. Dependence between consecutive days is the main free choice: the
  pre-registration fixes one primary assumption and one sensitivity check.
- **Period alignment:** NBP's 12Z-to-12Z amounts are re-apportioned to climate
  days using the NBS 6-hour snowfall (`S06`) shares where available, otherwise
  by hours of overlap.
- **Climatology:** resampled station-days for the remaining dates.
- Output: P_nbm(Total > K) per market per decision time, monotone across strikes
  by construction.

The climatology-only version of the same pipeline is the second baseline.

### Step 5. Compare (the registered analysis)

- Decision times: four per day, each a fixed lag after an NBM cycle.
- Market probability: mid of the last hourly bid/ask candle that closed before
  the decision time, only where a two-sided quote exists.
- Scores: Brier (primary), log loss, reliability diagrams, Murphy decomposition,
  for market, NBM composite and climatology.
- Uncertainty: block bootstrap where a block is one calendar week across ALL
  cities (so a storm that hits four cities is one block), 10,000 resamples.
- Strata, reported separately: storm-window versus monthly; lead time bands;
  NBM version.

### Step 6. Naive taker simulation

- At each decision time, buy YES if P_nbm - ask > tau after fees, buy NO if
  bid - P_nbm > tau after fees. Primary tau = 0.10; also 0.05 and 0.15.
- Size limited to a fixed fraction of that hour's traded volume, capped per
  market and per event. Hourly candles give no depth, so historical size is an
  estimate. The live logger's real depth will replace it for this season.
- One position per market per day, held to settlement.
- Report: fee-adjusted P&L, number of independent blocks, max drawdown,
  achievable size, and the correlation between perceived edge and realized edge.

## Part 2B: is NBM calibrated against the CLI measurement? (large sample)

Independent of Kalshi, and where the statistical power is.

- Stations: every first-order CLI station with reliable snowfall reports (target
  100 or more), winters 2020-21 to 2025-26.
- Targets: 24-hour, 48-hour and 72-hour CLI snowfall above 0.1, 1, 2, 4, 6, 8
  and 12 inches.
- Forecasts: NBP percentiles at fixed leads, same parser as 2A.
- Questions: Is NBM reliable against the manual CLI measurement? Where does it
  fail (marginal temperatures, heavy tails, particular regions)? Did reliability
  change across versions 4.0 to 5.1?
- This tells us whether "trade NBM against the market" is even a calibrated
  baseline, and sets the bar a Phase 4 model has to clear.

2B can run in parallel with 2A and does not need the pre-registration to be
final, because it never looks at prices.

## Order of work and effort

| Step | What | Rough effort | Runs on |
| - | - | - | - |
| 0 | Owner signs off plan and pre-registration | owner | |
| 1 | Kalshi settled-market pull | 1 day | laptop |
| 2 | Settlement replication to 100% | 1 to 2 days | laptop |
| 3 | NBM text pull, parser, availability times | 2 to 3 days | laptop |
| 4 | NBM-to-strike probability | 2 days | laptop |
| 5 | Registered comparison | 1 day | laptop |
| 6 | Taker simulation | 1 day | laptop |
| 2B | NBM calibration study | 3 to 4 days, parallel | laptop |

No laptop job needs to run for more than a few hours at a time. Downloads are
resumable and cached per station-cycle.

## Known weaknesses, stated up front

- Hourly candles are not order books. Quoted bid/ask at the hour close can be
  stale or one contract deep. Size estimates for last winter are soft.
- Tens of independent events. A true edge of a few points will not be
  distinguishable from zero here.
- The composite for monthly contracts leans on climatology beyond day 9, so
  early in a month it mostly tests climatology against the market.
- Last winter's markets used different strikes and a different settlement
  wording than this winter's.
- One winter of storm markets (6 events) cannot represent winters in general.
