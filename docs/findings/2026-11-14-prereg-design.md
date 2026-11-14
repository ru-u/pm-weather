# 2026-11-14: design analysis for three pre-registration choices

The owner asked for the three open choices in experiment P2A to be settled by
analysis. This note records what was looked at, what it showed, and the values
chosen. Reproduce with `uv run python scripts/prereg_design.py pull` then
`analyze` (seeded; numbers below are from that output).

**What this analysis used:** hourly bid, ask and volume candles of settled Kalshi
snow markets, their listing dates, and simulation. **What it did not use:** any
forecast, any outcome, any score of a price against an outcome. The registered
comparison is still unseen.

Sample: 382 settled markets in 60 monthly events and 6 storm-window events,
130,549 market-hours from November 2021 to April 2026 (Chicago December 2025
excluded, as pre-registered). At the four daily decision times, with the last
trade between 0.05 and 0.95 (outcome still open): 15,546 observations.

## Choice 1: maximum quoted spread for a usable market quote. Keep 0.20.

| Max spread | Observations kept | Monthly events kept | Storm events kept | Effective weeks |
| -: | -: | -: | -: | -: |
| 0.05 | 59% | 58 of 60 | 5 of 6 | 18.0 |
| 0.10 | 81% | 59 of 60 | 5 of 6 | 20.0 |
| 0.15 | 84% | 59 of 60 | 6 of 6 | 20.3 |
| **0.20** | **86%** | 59 of 60 | 6 of 6 | 20.5 |
| 0.30 | 88% | 60 of 60 | 6 of 6 | 20.7 |
| none (any two-sided quote) | 91% | 60 of 60 | 6 of 6 | 21.3 |

- The median spread is 0.04 in both market types. Most quotes are tight.
- Scoring the mid instead of the market's unknown true belief inside the spread
  costs at most s²/12 in Brier score, under 0.001 at every threshold. So the
  filter is not needed for accuracy. Its job is to drop stale placeholder quotes.
- Wide quotes are the quiet hours, not the busy ones. Mean hourly volume in
  monthly markets falls from 154 contracts (spread up to 0.05) to 47 (0.20 to
  0.30) and 30 (above 0.30). Dropping them does not discard the active periods.
- Below 0.15 one of the six storm events drops out entirely. With six storm
  events, that is too expensive.
- Between 0.15 and 0.30 nothing material changes: 84% to 88% of observations
  and the same effective sample.

**Decision:** 0.20, as drafted. It is also twice the primary trading threshold
(tau = 0.10), so the mid is never further from a tradable price than tau. The
result is insensitive in this range, so 0.10 and 0.30 are registered as
sensitivity checks: if the sign of the primary metric differs between them, the
outcome is CONDITIONAL whatever the primary says.

## Choice 2: pool storm-window and monthly markets. Yes, and be clear about what that means.

| Sample | Observations | Calendar weeks | Effective weeks | False-signal rate of a 95% interval (true effect zero) |
| - | -: | -: | -: | -: |
| Pooled | 13,377 | 81 | 20.5 | 7.4% |
| Monthly only | 13,138 | 81 | 20.7 | 6.3% |
| Storm only | 239 | 4 | 2.3 | 22.7% |

- **Storm-only cannot be the primary metric.** The six storm events sit in four
  calendar weeks (three weather episodes: late January, early February, late
  February 2026). A bootstrap over four blocks reports a "significant" result
  23% of the time when nothing is there. No threshold fixes that; the data do
  not exist yet.
- **Pooled is, in practice, a test of monthly markets.** Storm windows are 1.8%
  of pooled observations but 42% of all contracts traded. And 62% of all
  observations come from January to March 2026, when eleven cities were listed;
  2021 to 2023 contribute 3%.
- Weighting by volume instead would leave 6.5 effective weeks, with one week
  carrying 22% of the weight. Weighting each event equally gives 27 effective
  weeks instead of 20.5, not enough of a gain to justify a less transparent
  metric.
- With about 20 effective weeks the 95% interval is slightly optimistic: it
  signals falsely about 7% of the time instead of 5% (two-sided).

**Decision:** pooled, observation-weighted, as drafted. Three additions to the
registration:
1. The storm-window stratum is reported as point estimates only, with the
   statement that no inference is possible from four weeks.
2. A GO from the pooled metric licenses Phase 4 work on **monthly** markets. It
   says nothing about storm windows.
3. The storm-window question is answered prospectively: this winter's logged
   order books and settlements are the sample, scored under the same rule once
   at least 12 storm events in at least 8 distinct weeks have settled.

That last point is another reason the live logger matters more than anything
else this month.

## Choice 3: share of bootstrap resamples with positive simulated P&L for a GO. Keep 90%.

Chance that the full GO rule fires (Brier interval above zero AND P&L positive
in at least T of resamples), using the real weekly block structure, heavy-tailed
week effects, and effects stated as expected t-statistics:

| Scenario | Brier rule alone | T = 75% | **T = 90%** | T = 95% | T = 97.5% |
| - | -: | -: | -: | -: | -: |
| No edge at all | 3.6% | 2.7% | **1.4%** | 1.0% | 0.8% |
| NBM more accurate, but fees eat the edge (P&L zero) | 74.7% | 23.7% | **11.4%** | 6.1% | 3.3% |
| NBM more accurate, P&L negative | 71.7% | 4.5% | **1.1%** | 0.4% | 0.1% |
| Modest real edge (t about 2) | 59.7% | 57.7% | **54.1%** | 49.3% | 41.5% |
| Clear real edge (t about 3) | 85.1% | 84.5% | **83.0%** | 80.3% | 74.9% |
| Real edge, weaker in P&L than in accuracy | 85.5% | 74.1% | **59.5%** | 48.1% | 36.9% |

- The P&L condition earns its place: without it, "NBM is more accurate but you
  cannot make money after fees" would produce a GO three times in four.
- Raising T from 90% to 95% halves the false GO in that scenario (11% to 6%)
  but costs 5 to 11 points of power when the edge is real. Lowering it to 75%
  doubles the false GO (24%) for 1 to 15 points of power.
- Which is better depends on the cost of each mistake. A false GO costs a few
  weeks of modeling and paper trading with no capital at risk, and later gates
  catch it. A missed GO lands in CONDITIONAL, which still allows modeling if
  P2B finds an NBM weakness, but it slows work inside the one winter window.
  Taking a missed GO as one to two times as costly as a false GO, with prior
  weights of 25% real edge, 35% accurate-but-unprofitable and 40% nothing, the
  expected cost is lowest at T = 90% (and within noise of 95% at a ratio of
  one). T = 95% only wins if a false GO is the more expensive mistake. Those
  priors are a judgment, not a measurement.
- Simulated P&L is concentrated: 6.5 effective weeks by volume. The P&L
  condition is therefore the weak link in power, which argues against making
  it stricter.

**Decision:** 90%, as drafted.

## What the analysis says beyond the three choices

- Historical data can answer the monthly-market question with moderate power
  and cannot answer the storm-window question at all.
- Nearly half the money last winter was in storm windows. If there is an edge
  worth having, that is probably where it is, and only live logging will show it.
