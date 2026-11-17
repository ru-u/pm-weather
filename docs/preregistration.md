# Pre-registration

Hypotheses and decision rules, written before results are seen. Deviations are
logged at the bottom with a date and a reason.

---

## Experiment P2A: Kalshi snow market versus NBM guidance

**Status: FROZEN 2026-11-14. RUN 2026-11-15: CONDITIONAL (see Results).** The owner delegated the three open
design choices to analysis ("do the research and figure out the optimal"); they
are resolved below and in `docs/findings/2026-11-14-prereg-design.md`. From here
on, any change is a deviation and is logged at the bottom.

What has been looked at before freezing: settlement outcomes of settled markets;
total volumes per series; one NBM bulletin, to learn its format; and hourly bid,
ask and volume candles of settled snow markets, used only for spread, coverage
and block-structure statistics. No forecast has been joined to a price and no
price has been scored against an outcome.

### Question

At matched timestamps, is the Kalshi market mid a better, equal or worse
probability forecast of snow-contract outcomes than a forecast built from NBM
station guidance, observed snowfall to date, and climatology?

### Sample (fixed before analysis)

- Markets: all settled binary snow-total markets in `KX*SNOWM`, `KXSNOWNYM`,
  `SNOWNYM` and `KXSNOWSTORM` whose settlement we reproduce from NWS CLI data.
- Excluded in advance: ski-resort series; Chicago December 2025; any market
  whose settlement cannot be reproduced (listed with reasons before scoring).
- Observations: one per (market, decision time) where the market had a
  two-sided quote with spread at most 0.20.
- Decision times: 02:30, 08:30, 14:30 and 20:30 UTC, using the latest NBM cycle
  whose `available_at` is earlier than the decision time.
- Period: from each market's open until the earlier of its close and the moment
  its outcome is already determined by observed snowfall.

### Forecasts being compared

1. **Market:** mid of the last hourly bid/ask candle closed before the decision time.
2. **NBM composite:** observed-to-date plus NBP 24-hour snow percentiles plus
   station climatology beyond the NBM window, as specified in
   `docs/phase2_plan.md` step 4. Primary dependence assumption between
   consecutive days: rank correlation 0.5. Sensitivity: independent days.
3. **Climatology:** the same pipeline with climatology for every unobserved day.

No parameter of forecast 2 or 3 is fitted on Kalshi outcomes.

### Primary metric

Difference in mean Brier score, market minus NBM composite, over all
observations: `D = Brier(market) - Brier(NBM)`. Positive D means NBM is better.

Confidence interval: block bootstrap, one block per calendar week across all
cities and series, 10,000 resamples, 95% percentile interval.

### Secondary metrics (reported, not decisive alone)

Log loss; reliability diagrams and Murphy decomposition; D by stratum
(storm-window versus monthly; lead time under 3 days, 3 to 9 days, over 9 days;
NBM version); taker simulation at tau = 0.10 (also 0.05, 0.15) after fees with
volume-capped size; correlation between perceived and realized edge.

### Decision rule

| Outcome | Condition | Action |
| - | - | - |
| **GO** | 95% CI for D is entirely above 0, AND the tau = 0.10 taker simulation has after-fee P&L above 0 in at least 90% of bootstrap resamples | Free information is not priced in. Proceed to Phase 4 for snow and to paper trading design. |
| **NO GO** | 95% CI for D is entirely below 0, AND the taker simulation's after-fee P&L is not above 0 in at least 50% of resamples | The market beats NBM. Stop snow modeling. Move effort to ERCOT and power. Keep logging. |
| **CONDITIONAL** | Anything else, including a CI that spans 0 | Edge, if any, must come from a model that beats NBM. Phase 4 for snow starts only if experiment P2B shows a specific, sizeable NBM weakness, and that model must beat NBM out of sample before any paper trading. |

A CONDITIONAL result is the expected one given the sample size. It is not a
soft GO.

### Power, stated honestly

With on the order of 20 to 40 effective weekly blocks, the test can separate
market and NBM only if their Brier scores differ by several hundredths. A
difference of 0.01 will not be detected. No conclusion about small edges will be
drawn from P2A in either direction.

### Guardrails

- Automated leakage test: every NBM value joined to an observation satisfies
  `available_at < decision_time`; every observed snowfall value used was
  published before the decision time.
- The analysis code is run end to end on a synthetic data set with a known
  answer before it is run on real prices.
- All strata and thresholds above are the complete list. Anything else found is
  labelled exploratory.
- Results are written to `docs/findings/` whatever they show.

### Design choices, resolved 2026-11-14

1. **Usable quote:** two-sided, spread at most 0.20. Registered sensitivity
   checks at 0.10 and 0.30: if the sign of D differs between them, the outcome
   is CONDITIONAL regardless of the primary result.
2. **Pooling:** storm-window and monthly markets are pooled, observation-weighted,
   for the primary metric. The storm-window stratum is reported as point
   estimates only (six events in four calendar weeks support no inference). A GO
   licenses Phase 4 work on monthly markets only.
3. **P&L threshold for GO:** positive in at least 90% of bootstrap resamples.

Known property of the test, measured by simulation on the real block structure:
about 20 effective weekly blocks; the 95% interval signals falsely about 7% of
the time two-sided; a GO fires about 1% of the time with no edge, about 11% of
the time when NBM is more accurate but fees consume the edge, and about 54% and
83% of the time for real edges with expected t-statistics of 2 and 3.

### Prospective extension: storm-window markets (P2A-S)

History cannot answer the storm-window question, and storm windows carried 42%
of contracts traded. The same comparison, with the same rule, is run on storm-
window markets logged live from 2026-11 onward, once at least 12 storm events in
at least 8 distinct calendar weeks have settled. Market quotes come from the
logger's order books instead of hourly candles. Nothing is scored before then.

#### P2A-S implementation clarifications (2026-11-17, before any sample market opened)

Written with the scoring code (`pmw.eval.p2as`), before any storm-window market
in the sample existed. The only storm event logged so far (KXSNOWCITIES-26NOV15)
opened before the logger and is outside the sample; it was used only to run the
pipeline end to end (`--dev`), and no observation survived the quote filter.
These fix what the paragraph above leaves open; they are not deviations.

1. **Series.** KXSNOWSTORM and KXSNOWCITIES (a newer storm-window series that
   lists several cities under one event). Everything else in P2A's sample rule
   applies: station named in the rules and configured, "greater" strikes,
   settlement reproduced by `pmw.settle.snow`.
2. **Logged live.** A market is in the sample if it opened at or after the
   logger's start, 2026-11-14 16:12 UTC, so its whole life is in the books.
3. **Events.** An event is a Kalshi event ticker; a KXSNOWCITIES event counts
   once. Its composite is built per station, with P2A's code unchanged.
4. **Quote.** The latest logged book received at or before the decision time
   and at most 10 minutes old; no such book means no observation, as a missing
   candle did. Bid and ask are the best YES bid and 1 minus the best NO bid. The
   two-sided, spread and sensitivity rules are P2A's.
5. **Volume for sizing.** Contracts in the logger's trade prints during the hour
   P2A's candle covered (13:00 to 14:00 UTC for a 14:30 decision).
6. **Fills.** P2A's sizing rule (clarification 10) sets the cap; the order
   then walks the logged book level by level while the after-fee edge stays
   above tau, using the shared `pmw.backtest.taker.walk`. Within an event and
   decision time, the largest edge at the best level goes first.
7. **When it is scored.** Once, on or after 2027-05-15, on every sample market
   settled by then, provided at least 12 events with windows starting in at
   least 8 distinct ISO weeks have a settled, reproduced market. If the gate is
   not met, the sample carries over and the same check is made after the next
   winter (on or after 2028-05-15). The code refuses to score before that
   (owner decision, D-054).
8. **Inputs** are rebuilt from the archives after the season
   (`make live-snow-inputs`), and P2A's leakage check must pass. Blocks, seed,
   resamples, taus and the decision rule are P2A's.

### Implementation clarifications (2026-11-15, before any scoring)

Details the frozen text leaves open, fixed while building the pipeline and
before any market price was compared with any forecast or outcome. At this
point stage 1 (forecasts only) had been run as a trial; no quote had been
joined. These are not deviations.

1. **Market quote.** The hourly candle ending at the top of the hour before the
   decision time (the 14:00 candle for 14:30), its closing YES bid and ask. No
   candle for that hour means no observation. This is the reading used in the
   design analysis.
2. **When an observation counts as public.** A climate day's snowfall is usable
   8 hours after the climate day ends (about 8 AM local the next morning).
   Only an unbroken prefix of the window is treated as observed.
3. **NBM forecast used.** For each 24-hour period, the most recent cycle whose
   `available_at` is strictly before the decision time (`available_at` = S3
   upload time if within 3 hours of the cycle, else cycle + 2 hours).
4. **Per-period distribution.** Piecewise-linear quantile function through
   (0, 0) and the five percentiles; exponential tail above the 90th with scale
   (q90 - q75) / ln 2.5, floored at 0.1 in.
5. **Climatology.** Whole-season resampling (all days of a draw come from one
   historical season), from seasons strictly before the contract's season,
   station history from ACIS since 1991-07. A missing station-day is filled
   from the same calendar day in another season.
6. **Strike rounding.** "Strictly greater than K" with totals reported to
   0.1 in is evaluated as a continuous total of at least K + 0.05.
7. **Determined observations** (excluded): observed snowfall public at t
   already exceeds the strike, or the whole window is observed.
8. **Log loss** uses probabilities clipped to [0.01, 0.99] for every forecast
   (market, NBM, climatology). Brier scores are unclipped.
9. **Strata.** Lead time = end of the contract window minus the decision time.
   NBM version = the versions of the forecasts used at that decision time,
   "mixed" when more than one.
10. **Taker simulation sizing.** 10% of the decision hour's traded volume (from
    the same hourly candle), at most 250 contracts per market per UTC day and
    1,000 per event per UTC day, filled at the candle's closing ask (YES) or bid
    (NO), Kalshi taker fees from `pmw.markets.fees` (quadratic, multiplier 1),
    one position per market per UTC day, held to settlement. Within an event and
    decision time, the largest perceived edge is filled first.
11. **P&L bootstrap.** The same weekly blocks as the Brier metric (every week
    with at least one observation; weeks without trades contribute zero),
    10,000 resamples, seed 20261115.
12. **Sensitivity rule** (already registered): if the sign of D at spread 0.10
    or 0.30 differs from the primary, the outcome is CONDITIONAL. The
    independent-days NBM variant is reported, not decisive.
13. **Synthetic check.** Before the real run, the scoring code is run on
    synthetic data where NBM is truth and the market noisy, and the reverse,
    and must return GO and NO GO respectively.

---

## Experiment P2B: NBM calibration against CLI snowfall (no prices)

**Status: FROZEN 2026-11-17 04:56 UTC. Drafted 2026-11-14; design resolved 2026-11-17
(below), before any scoring.** Descriptive study with one registered test. It informs the snow
storm-window model (Phase 4, D-040): where NBM's snowfall probabilities are
miscalibrated, and whether a simple correction learned on past winters holds up
on a held-out winter. No prices.

### What changed from the 2026-11-14 draft, and why (before scoring)

- Stations: the 14 snow settlement stations of `configs/stations.yaml` (NBP is
  pulled for these), not "all first-order CLI stations" (hundreds of stations
  and about 700,000 archive requests, for stations we do not trade).
- Winters: 2021-22 to 2025-26 (November to March). NBM 24-hour snowfall
  percentiles in the archive start with V4.0; our pull starts 2021-11-01.
- Windows are climate days (local standard midnight to midnight), as Kalshi
  settles, so NBM's 12Z-to-12Z periods are mapped with the P2A composite
  (`models/nbm_composite.py`: NBS 6-hour shares for partial periods, Gaussian
  copula rho_S 0.5 between periods). Scoring raw 12Z periods against CLI days
  would mismatch the windows by 7 hours and make NBM look overconfident.

### Sample

- Every (station, window start D, window length n, lead L) with D from November 1
  to March 31 of each winter, the whole window inside that range, n in {1, 2, 3}
  climate days, L in {1, 2, 3, 5, 7} days. Decision time t = start of climate
  day D (UTC) minus L days; the forecast uses only NBM cycles public before t
  (`available_at`). Nothing in the window is observed at t.
- Kept only if the composite uses no climatology (NBM reaches the end of the
  window) and every day of the window has an observed value.
- Outcome: CLI daily snowfall (IEM), ACIS where CLI is missing, as in P2A
  (`features/obs_table.py`), rebuilt for the 14 stations in a separate file so
  P2A's table is unchanged. Trace counts as 0.
- Thresholds K: total above 0.1, 1, 2, 4, 6, 8 and 12 inches ("above" strictly,
  as Kalshi; the composite's `prob_above`).

### Splits

Development: winters 2021-22 to 2024-25. Test: 2025-26, scored once. Caveat:
P2A scored this composite on 2025-26 Kalshi events at these stations, so
2025-26 is not untouched; P2A did not look at recalibration.

### Reported (descriptive, development winters)

Per threshold x lead x window length: cases, base rate, mean forecast, Brier,
Brier skill against station climatology (the composite with no NBM, whole-season
resampling from earlier seasons), reliability (10 bins) and Murphy terms; the
same by NBM version; the logistic recalibration coefficients below (slope below 1
means overconfident, intercept the bias).

### Registered test

Recalibration fitted on development winters, separately for each (threshold,
window length), pooled over leads and stations: logit p' = a + b logit p, with p
clipped to [0.001, 0.999], maximum likelihood. On 2025-26:
`dB = Brier(raw NBM) - Brier(recalibrated)` per (case, threshold), averaged over
all cases and thresholds; 95% interval by block bootstrap over ISO weeks of the
window start (all stations together, since one storm moves several cities),
10,000 resamples, seed 20261118. **A documented NBM weakness** (the term in
D-038 and the Phase 2 rule) is: interval entirely above 0. Also reported per
threshold, per lead and per window length (not decisive).

### Implementation clarifications (at freezing, nothing scored)

1. Code: `src/pmw/eval/p2b.py`, run once with `uv run pmw p2b`; outputs in
   `data/processed/p2b/`. It stops before scoring if a leakage count is not 0.
2. Observed table: `pmw obs-build --all-stations --out
   data/interim/obs/daily_snow_p2b.parquet` (CLI 24,918 station-days, ACIS 1,172,
   missing 2,008). Chicago Midway (KMDW) has no snowfall in its climate report or
   in ACIS, so every KMDW window drops under the "every day observed" rule: 13
   stations in practice.
3. Climatology probabilities use the composite with no NBM at the same t
   (whole-season resampling from earlier seasons), as in P2A.
4. Synthetic known-answer runs (`tests/test_p2b.py`): forecasts with log-odds
   stretched 2.2x are flagged (slope below 0.7, weakness documented);
   calibrated forecasts are not.

### Guardrails

- Leakage: every NBM cycle used has `available_at` before t; no window day is
  observed at t (count must be 0).
- Recalibration fitted on development winters only; the test winter is read
  once, after the code runs on a synthetic set with a known answer
  (overconfident forecasts must be detected, calibrated ones must not).
- Results go to `docs/findings/` whatever they show.

---

## Experiment P3A: Kalshi ERCOT daily peak versus ERCOT's own load forecast

**Status: FROZEN 2026-11-16 02:45 UTC. RUN 2026-11-16 05:08 UTC: CONDITIONAL
(see Results).** Frozen before any NP3-565-CD forecast was
parsed, before any forecast was joined to a price, and before any forecast was
scored against an outcome. Design choices were made by Claude while the owner
was away (the owner asked for work to continue); the owner may review them, and
any change from here on is a deviation logged at the bottom.

What has been looked at before drafting: the rule text, results and expiration
values of every settled KXTXERCOTPEAKD market (to check our reading of the
contract); volume per event; ERCOT archive extent and one NP6-346-CD and one
NP3-565-CD document (format only). No forecast has been joined to a price and
no forecast has been scored against an outcome.

### Question

At fixed times on the operating day, is the Kalshi mid a better, equal or worse
probability forecast of "first-complete NP6-346-CD daily peak above the strike"
than a probability built from ERCOT's own published seven-day load forecast?

### Sample (fixed before analysis)

- Markets: every settled KXTXERCOTPEAKD market with operating day 2026-08-28
  to 2026-11-14 (the last settled day at freezing) whose settlement we reproduce from first-complete NP6-346-CD
  peaks. Markets we cannot reproduce are listed with reasons before scoring and
  excluded.
- Observations: one per (market, decision time) where the last hourly candle
  ending at or before the decision time is two-sided (bid >= 0.01, ask <= 0.99,
  ask > bid) with spread at most 0.20.
- Decision times: 00:00, 06:00, 10:00 and 14:00 Central prevailing time on the
  operating day (markets open 23:00 CT the evening before and close 23:55 CT).

### Forecasts being compared

1. **Market:** mid of that candle's closing bid and ask.
2. **ERCOT forecast probability:** take the latest NP3-565-CD posting whose
   `postDatetime` is before the decision time (postings kept at 23:30 the
   evening before, 05:30, 09:30 and 13:30 CT). F = the maximum hourly
   `SystemTotal` of the model flagged `InUseFlag = Y` over the operating day.
   Error ratio r = A / F, where A is the first-complete NP6-346-CD peak.
   **Training set:** every operating day from 2017-07-01 to 2026-08-27 (before
   the first Kalshi market), same posting slot, day of year within 30 days of
   the target day (circular). P(peak > K) = share of training ratios r with
   F x r > K. No parameter is fitted on Kalshi prices or test-period outcomes.

### Primary metric

`D = Brier(market) - Brier(ERCOT forecast)` over all observations; positive D
means the ERCOT forecast is more accurate. 95% interval: block bootstrap, one
block per ISO calendar week (Central time), 10,000 resamples, seed 20261116.

### Secondary metrics (reported, not decisive alone)

Log loss (probabilities clipped to [0.01, 0.99]); reliability and Murphy
decomposition; D by decision time; D on the 20% of operating days with the
highest peaks; a 17:00 CT decision time using the 16:30 posting, reported
separately; taker simulation at tau = 0.10 (also 0.05, 0.15) after fees, with
the P2A rules (size = 10% of the candle hour's volume, at most 250 contracts per
market and 1,000 per event per day); perceived versus realized edge.
Sensitivity: training on the last three pre-Kalshi years only
(2023-08-28 to 2026-08-27).

### Capacity (Phase 3 gate)

"About $20k a day on stressed days." Stressed days: the 20% of operating days in
the sample with the highest first-complete peak. Volume: contracts traded per
event (each contract pays $1), from Kalshi's market records. Gate met if the
median stressed day traded at least 20,000 contracts. The dollar premium
(contracts x price) is reported alongside.

### Decision rule

| Outcome | Condition | Action |
| - | - | - |
| **GO** | 95% CI for D entirely above 0, AND tau = 0.10 taker P&L above 0 in at least 90% of resamples, AND the capacity gate is met | Phase 4 for ERCOT: model design, then paper trading design. |
| **NO GO** | 95% CI for D entirely below 0, AND taker P&L above 0 in fewer than 50% of resamples | The market already beats ERCOT's own forecast. No ERCOT model; keep logging. |
| **CONDITIONAL** | Anything else, including a CI spanning 0, a sign change across the spread sensitivities (0.10 / 0.30) or the training sensitivity, or accuracy and P&L passing with capacity failing | Prospective test on live-logged books through summer 2027 (P3A-S, registered separately). Phase 4 only with a specific, documented forecast weakness. |

### Power, stated honestly

About 78 operating days in about 12 calendar weeks. Weekly blocks are the honest
unit (weather regimes last days), and with about 12 blocks percentile
intervals tend to be too narrow, not too wide. Only a large difference (several
hundredths of Brier score) could be detected. The busy summer days are only the
first five weeks of the sample.

### Design facts (2026-11-16, quotes only, no forecast or outcome joined)

Hourly candles for all 1,334 settled markets (1,221 from the live tier, 113
historical). Share of (market, decision time) pairs with a usable quote:

| CT hour | two-sided | spread <= 0.10 | <= 0.20 | <= 0.30 | median spread |
| - | -: | -: | -: | -: | -: |
| 00 | 0.56 | 0.05 | 0.11 | 0.15 | 0.82 |
| 06 | 0.63 | 0.25 | 0.43 | 0.51 | 0.13 |
| 10 | 0.63 | 0.26 | 0.43 | 0.50 | 0.14 |
| 14 | 0.41 | 0.11 | 0.19 | 0.25 | 0.23 |
| 17 | 0.10 | 0.05 | 0.06 | 0.06 | 0.13 |

Primary sample at spread 0.20: about 1,551 observations, 76 operating days,
12 ISO weeks. The books are nearly empty just after the 23:00 CT open, and by
17:00 most markets are one-sided (decided or abandoned), which is why 17:00 is
secondary only. No design choice was changed after seeing these numbers.

### Implementation clarifications (2026-11-16, at freezing)

1. Settlement replication (rule 6) passed before freezing: 1,334 of 1,334
   (D-036). No market is excluded.
2. The forecast for a decision time is the latest in-use posting for that
   operating day posted strictly before it. If that posting is not one of the
   kept slots (for example the 23:30 posting is missing, so the latest is the
   previous afternoon's), the observation is dropped, not filled.
3. Training ratios use, per (operating day, slot), the latest posting in that
   slot, and the first-complete NP6-346-CD peak as A. Training days with no
   in-use posting in a slot are skipped.
4. The quote is the hourly candle whose end time equals the decision time,
   closing bid and ask; the taker's volume is that candle's volume.
5. Capacity uses each market's total `volume_fp`; the dollar premium is
   approximated with the market's last price and is descriptive only.
6. Code: `src/pmw/eval/p3a_score.py`, synthetic known-answer runs in
   `tests/test_p3a.py` (GO, capacity-limited CONDITIONAL and NO GO all come out
   as constructed). Run once with `uv run pmw p3a`.

### Guardrails

- Automated leakage test: every posting used has `postDatetime` before the
  decision time; every training day is before 2026-08-28.
- The scoring code runs on a synthetic data set with a known answer before it
  runs on real prices.
- The strata and sensitivities above are the complete list. Anything else is
  exploratory.
- Results are written to `docs/findings/` whatever they show.

## Phase 4 experiments: registered privately, committed by hash

The full texts of the four Phase 4 experiments (models, decision rules and
execution rules) are kept in a private repository until each one is evaluated.
Each was frozen before its data was scored; the sha256 of each text was
published here on the day the repository was split (2026-11-17), so the text
can be revealed later and checked against this commitment. See
`docs/commitments.md` for how to verify.

### Experiment P4E: frozen ERCOT nowcast (v2) versus the Kalshi market

Frozen 2026-11-17 04:20 UTC. Settled KXTXERCOTPEAKD markets with operating day
2026-08-28 to 2026-11-13; is the frozen nowcast (D-044) a better probability
forecast than the Kalshi mid, and would a naive taker have made money after
fees? Run once (see Results). Text sha256 `d57914df4968862ebd651b80a750f43f33bfacd776628f0c07268e8960e3df02`.

### Experiment P4-paper: frozen ERCOT nowcast on live-logged books (prospective)

Frozen 2026-11-17 04:45 UTC, before its first operating day. KXTXERCOTPEAKD
markets with operating day 2026-11-18 to 2027-09-30, on books logged every 5
minutes; also serves as P3A-S (the market against ERCOT's own forecast).
Evaluated once, on or after 2027-10-15; data-health counts only until then.
Text sha256 `ad023f9f9a5253cc0a5973a1939068f264fb5641db22f420b1cea0e44e69050f`.

### Experiment P4S-T: frozen snow nowcast versus the composite, winter 2025-26 (no prices)

Frozen 2026-11-17 14:25 UTC. On the 2025-26 winter, which no model choice used,
is the frozen storm-window nowcast a better probability forecast than the P2A
composite? Run once (see Results). Text sha256 `e81c0225541654cba5dc55957138b7a0a9c67c28ba222b59e18388de7ea4b0e1`.

### Experiment P4S-paper: frozen snow nowcast on live-logged storm-window books (prospective)

Frozen 2026-11-17 14:25 UTC, amended the same day before any sample market
opened (see Deviations). Storm-window snow markets with windows from 2026-11-18
to 2027-04-30, on books logged every 5 minutes. Evaluated once, on or after
2027-05-15; data-health counts only until then. Text sha256 `df42ae563b20bd7c22df3c4eeb66b10df85b87a7fd00c387ed6a556711c31f90`.

---

## Results

- **P2A, run 2026-11-15 21:04 UTC: CONDITIONAL.** D = -0.0103 [-0.0167, -0.0029]
  (the market was more accurate than the NBM composite); taker P&L positive in
  62.9% of resamples. See `docs/findings/2026-11-15-p2a-result.md`.
- **P3A, run 2026-11-16 05:08 UTC: CONDITIONAL.** D = -0.0110 [-0.0178, -0.0035]
  (the market was more accurate than ERCOT's own forecast); taker P&L positive
  in 94.2% of resamples on 54 trades, with a perceived-versus-realized edge
  correlation of -0.33; capacity gate met (median stressed day 37,855
  contracts). See `docs/findings/2026-11-16-p3a-result.md`.
- **P4E, run 2026-11-17 04:24 UTC: CONDITIONAL.** D = -0.0053 [-0.0175, +0.0086]
  (the market was slightly more accurate than the frozen model; the sign flips
  with the spread filter); taker P&L +$325 on 47 trades, positive in 98.1% of
  resamples, perceived-versus-realized edge correlation +0.07. Details private
  until P4-paper is evaluated.
- **P2B, run 2026-11-17 04:57 UTC: no documented NBM weakness.** Recalibration
  fitted on 2021-22 to 2024-25 did not improve 2025-26: dB = -0.0001
  [-0.0017, +0.0014]. NBM skill against climatology on 2025-26 is 0.21 to 0.44 by
  threshold. See `docs/findings/2026-11-17-p2b-result.md`.
- **P4S-T, run 2026-11-17 14:27 UTC: CONFIRMED.** D = Brier(composite) −
  Brier(model) = +0.0081 [+0.0057, +0.0109] (the frozen nowcast was more
  accurate than the composite). Details private until P4S-paper is evaluated.

## Deviations

- **P4-paper, 2026-11-17, before its first sample day: interim results become
  visible** (D-055). Live paper trading of the same frozen model and rule starts
  with the sample, so P4-paper's paper P&L is seen as it accrues, against the
  registered "data-health counts only". The model and rule stay frozen; any change
  to either ends P4-paper's sample on the last day before it. Reason: owner
  decision to gain trading experience before the evaluation.
- **P4S-paper, 2026-11-17, before any sample market opened: primary model changed
  from v1 to v1.1** (a training-window selection problem found after P4S-T, D-051,
  D-052). v1 kept as a secondary arm. The amendment is part of the committed
  P4S-paper text.
- **P3A, 2026-11-16 02:50 UTC, before any forecast was joined to a price or
  scored: meaning of "the model flagged InUseFlag = Y".** The frozen text
  assumed one in-use model per posting, as in the one 2026 file seen. The
  2017-19 postings, read for format after freezing, flag one model per HOUR
  (0 of 57,600 hours have two), and the flagged model changes within the
  current day and between days. F is therefore the daily maximum of the
  hour-by-hour flagged values (`pmw.energy.ercot.in_use_daily_peaks`), which is
  the literal reading of the registered sentence. For 2026 postings, where one
  model is flagged on every hour, nothing changes. Reason: data format, not
  results.
- **P4E, 2026-11-17 04:22 UTC, first run crashed before any result was computed
  or seen: column name clash.** The model rows carry the regression target `y`
  (log ratio), which collided with the market outcome `y` in the join, so the run
  stopped with a KeyError while building the primary metric. Fixed by dropping the
  target column before the join, with a test that goes through
  `build_observations`. Nothing in the design changed. Reason: code error.
