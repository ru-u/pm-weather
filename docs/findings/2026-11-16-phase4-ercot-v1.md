# 2026-11-16: Phase 4 ERCOT nowcast, version 1 (validation, no prices)

Plan: `docs/phase4_ercot_plan.md`. Owner decision D-040. Run with
`uv run pmw ercot-model --rebuild`; full numbers in
`data/interim/ercot/peak_model_report.json`.

## Setup

- Target: the first-complete NP6-346-CD daily peak at decisions 00, 06, 10, 14
  and 17 CT, split into the peak already observed (real-time telemetry, NP3-910-ER)
  and the peak still to come (modelled as log of remaining-hours max over ERCOT's
  forecast for those hours).
- Rows: 16,712 (day, decision) pairs, 2017-07 to 2026-08-27. Train to 2023,
  calibrate on 2024, validate on 2025-01-01 to 2026-08-27 (3,015 rows). The
  Kalshi period was not used.
- Leakage check: 0 rows where the forecast posting is at or after the decision time.
- New data: 5-minute real-time load (NP3-910-ER), 3,455 days. Its hourly averages
  match settled hourly load to a median of 0.003% (90% of hours within ±0.7%), so
  "load so far" is a faithful stand-in for what settles. Early years are 15-minute,
  later 5-minute; files before 2018 are named `48h_*`.

## Validation results (Brier over 17 strikes, 500 MW apart, around ERCOT's forecast)

| | 00 CT | 06 CT | 10 CT | 14 CT | 17 CT | All | Hottest 20% of days |
| - | -: | -: | -: | -: | -: | -: | -: |
| Baseline (P3A method) | 0.112 | 0.104 | 0.096 | 0.090 | 0.051 | 0.090 | 0.092 |
| NGBoost (normal, sigma calibrated) | 0.105 | 0.099 | 0.082 | 0.067 | 0.018 | 0.074 | 0.077 |
| LightGBM quantiles + conformal | **0.103** | **0.095** | **0.081** | **0.064** | **0.018** | **0.072** | 0.078 |

LightGBM interval coverage on validation: 81.5% (nominal 80%), 95.5% (nominal 95%).

The improvement grows through the day (7% at midnight, 29% at 14:00, 65% at 17:00),
which is where P3A found the market's advantage over ERCOT's forecast.

## What drives it (share of gain, median LightGBM model)

| Feature | Share |
| - | -: |
| Mean of ERCOT's 8 model forecasts / in-use forecast | 16% |
| Current load / forecast for the remaining hours | 12% |
| Load ramp over the last hour | 11% |
| Last completed hour: actual / forecast | 9% |
| Forecast level | 8% |
| Season (day of year) | 11% |
| Recent bias (3, 7, 14 days) | 13% |

The two ideas from the write-up carry it: live "disagreement" between what is
happening and what was forecast, and combining several forecasts instead of
trusting one.

## What this does and does not show

- It shows a model that is much better than the P3A baseline, out of sample, on
  history that no choice was tuned on.
- It does not show an edge over the Kalshi market. In P3A the market beat the
  baseline by a comparable margin on its own sample. Whether this model beats the
  market is the next experiment (P4E), pre-registered before any price is joined.

## Next

1. Blend NGBoost and LightGBM in log-odds (weights fitted on validation), and add
   weather: NBM hourly temperature forecasts for Houston, Dallas–Fort Worth,
   Austin and San Antonio (2021 on) and observed temperatures so far.
2. Pre-register P4E: the chosen model against the Kalshi mid and against bid/ask
   at the same decision times on 2026-08-28 to 2026-11-14, run once, with the
   perceived-versus-realized edge correlation as a headline metric.
3. Live inputs for paper trading: log ERCOT's real-time dashboard load and each
   hourly forecast posting from `ercot-jobs` on a schedule.
