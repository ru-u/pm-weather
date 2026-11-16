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

**Status: DRAFT.** Descriptive study, so there is no go/no-go rule, but the
targets, thresholds, leads and station list are fixed before scoring:
24/48/72-hour CLI snowfall above 0.1, 1, 2, 4, 6, 8 and 12 inches; leads of 1,
2, 3, 5 and 7 days; all first-order CLI stations with at least 90% snowfall
report completeness in the winters used; winters 2020-21 to 2025-26; results
split by NBM version. Test winter held out: 2025-26, touched once.

---

## Experiment P3A: Kalshi ERCOT daily peak versus ERCOT's own load forecast

**Status: FROZEN 2026-11-16 02:45 UTC**, before any NP3-565-CD forecast was
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

---

## Results

- **P2A, run 2026-11-15 21:04 UTC: CONDITIONAL.** D = -0.0103 [-0.0167, -0.0029]
  (the market was more accurate than the NBM composite); taker P&L positive in
  62.9% of resamples. See `docs/findings/2026-11-15-p2a-result.md`.

## Deviations

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
