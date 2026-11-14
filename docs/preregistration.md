# Pre-registration

Hypotheses and decision rules, written before results are seen. Deviations are
logged at the bottom with a date and a reason.

---

## Experiment P2A: Kalshi snow market versus NBM guidance

**Status: FROZEN 2026-11-14. Not yet run.** The owner delegated the three open
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

---

## Experiment P2B: NBM calibration against CLI snowfall (no prices)

**Status: DRAFT.** Descriptive study, so there is no go/no-go rule, but the
targets, thresholds, leads and station list are fixed before scoring:
24/48/72-hour CLI snowfall above 0.1, 1, 2, 4, 6, 8 and 12 inches; leads of 1,
2, 3, 5 and 7 days; all first-order CLI stations with at least 90% snowfall
report completeness in the winters used; winters 2020-21 to 2025-26; results
split by NBM version. Test winter held out: 2025-26, touched once.

---

## Deviations

None yet.
