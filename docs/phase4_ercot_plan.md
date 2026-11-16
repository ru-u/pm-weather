# Phase 4, ERCOT: a nowcasting model for the daily peak (plan, 2026-11-16)

Owner decision D-040. Builds on P3A: the market beat ERCOT's own forecast, and
beat it most late in the day, when real-time load is visible. The model's job
is to see what the market sees, earlier and better calibrated.

## Target

For operating day D and decision time t (Central), the distribution of the
first-complete NP6-346-CD daily peak A. Decomposed so the intraday structure is
exact rather than learned:

- H(t) = highest hourly load already completed by t (from real-time telemetry).
- R(t) = highest hourly load in the hours still to come.
- A = max(H, R). So P(A > K) = 1 when H > K, else P(R > K).
- The model predicts the distribution of log(R / F_rem), where F_rem is ERCOT's
  in-use forecast maximum over the remaining hours. At 00:00 nothing is
  observed, so R = A and F_rem = F.

Decision times: 00:00, 06:00, 10:00, 14:00 and 17:00 CT, each using the latest
forecast posting before it (23:30 the evening before, 05:30, 09:30, 13:30, 16:30),
the same grid as P3A.

## Data (all public, backfilled)

| Source | Use | Period |
| - | - | - |
| NP6-346-CD hourly actuals, first complete | target A, R | 2017-06 to now |
| NP3-565-CD seven-day forecasts, 8 models + in-use | F, F_rem, model spread, revisions | 2017-06 to now, 5 postings a day |
| NP3-910-ER 5-minute telemetered load (system + 4 zones) | H, current load, ramp, observed-vs-forecast so far | 2017-06 to now |
| NBM station text (KIAH, KDFW, KAUS, KSAT) | hourly temperature forecast | 2021 to now (phase 2 of the model) |
| ASOS observations via IEM | temperature so far today | all years (phase 2) |

NP3-910-ER is published two days late, so in history it stands in for what
ERCOT's public dashboard showed in real time. Live, the model reads the
dashboard. The gap between 5-minute telemetry averages and settled hourly
values is measured on the overlap and reported.

## Features (version 1)

- Forecast: F_rem, F, all 8 model maxima, their spread (std, range), hour of the
  forecast peak, revision since the previous posting.
- Intraday ("disagreement", the write-up's key idea): H / F_rem, current load /
  forecast for the current hour, mean ratio observed / forecast over elapsed
  hours, last-hour ramp, hours until the forecast peak hour, zone shares.
- Recent bias: mean ratio of actual peak to the in-use day-ahead forecast over
  the previous 3, 7 and 14 days, using only days whose actuals were published
  before t (telemetry for yesterday).
- Calendar: day of year (sin, cos), day of week, holiday, DST.

Version 2 adds NBM hourly temperature forecasts and ASOS temperatures so far.

## Models, compared on the same splits

1. **Baseline:** P3A's empirical error ratios (slot, season).
2. **NGBoost** (normal on log ratio), as in the write-up.
3. **LightGBM quantile regression** (19 quantiles) with conformalized quantile
   regression for calibrated coverage.
4. **Blend** of 1 to 3 in log-odds space (weights fitted on validation only),
   the write-up's approach to combining a strong model with independent ones.

## Evaluation

- Train: operating days 2017-07-01 to 2024-12-31. Validation (model choice,
  blend weights, calibration): 2025-01-01 to 2026-08-27. Nothing from the Kalshi
  period (2026-08-28 on) is used for any choice.
- Metrics: CRPS of the peak; Brier score over a strike grid like Kalshi's
  (500 MW steps within ±4 GW of F); PIT histograms; coverage of 80% and 95%
  intervals; all by decision time and on the hottest 20% of days.
- Gate to the next step: the chosen model must beat the baseline on validation
  Brier at every decision time. Then a new experiment (P4E) is pre-registered:
  the model against the Kalshi market on 2026-08-28 to 2026-11-14 (run once),
  with the key metric the write-up also used, the correlation between perceived
  and realized edge, plus a prospective test on live books.

## Execution ideas to test later (on logged order books)

- Opening quotes at 23:00 CT (median spread 0.82): the write-up's edge was
  naive opening quotes.
- Quote staleness after each hourly forecast posting (:30) and after each
  completed hour of load.
- Maker quotes around fair value (no maker fee on these series).
