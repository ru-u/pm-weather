# 2026-11-16: Phase 4 ERCOT nowcast, version 2 (weather; validation, no prices)

Follows v1 (`2026-11-16-phase4-ercot-v1.md`, D-041). Run with
`uv run pmw ercot-model-v2 --rebuild-weather`; full numbers in
`data/interim/ercot/peak_model_v2_report.json`. Same rows, target, splits
(train to 2023, calibrate 2024, validate 2025-01-01 to 2026-08-27, 3,015 rows)
and strike grid as v1. The Kalshi period was not used.

## New inputs

- **NBM hourly bulletins (NBH)** for Houston (KIAH), Dallas-Fort Worth (KDFW),
  Austin (KAUS), San Antonio (KSAT) and Midland (KMAF), 2020-05-18 (start of the
  AWS archive) to 2026-11-15. For a decision at time t the cycle issued at t − 2 h
  is used, and only if its archive timestamp is before t. 98.6% to 99.9% of rows
  from 2021 on have it; rows before 2020-05 have no NBM features (the trees treat
  them as missing).
- **ASOS observations** (IEM) for the same stations, 2017 on, each usable 15
  minutes after its observation time. All four zones are observed in 99.4% of
  hours; Midland has outages (about 2% of hours in 2024-25).
- Stations stand for ERCOT's four forecast zones (South = mean of Austin and San
  Antonio), weighted by each zone's share of settled load over the 90 days ending
  two days before the operating day. Midland was added because West's share grew
  from 10% (2018) to 17% (2025).
- Features: load-weighted forecast temperature over the remaining hours (max,
  mean, cooling and heating degree hours, NBM's own spread), latest observed
  temperature, observed minus forecast since the cycle, warmest hour so far, and
  forecast max minus current temperature.

## Validation results (Brier over 17 strikes, 500 MW apart)

| | 00 CT | 06 CT | 10 CT | 14 CT | 17 CT | All | Hottest 20% |
| - | -: | -: | -: | -: | -: | -: | -: |
| Baseline (P3A method) | 0.112 | 0.104 | 0.096 | 0.090 | 0.051 | 0.090 | 0.092 |
| LightGBM v1 | 0.103 | 0.095 | 0.081 | 0.064 | 0.018 | 0.0721 | 0.0775 |
| **LightGBM v2 (weather)** | **0.102** | **0.093** | **0.080** | **0.064** | **0.017** | **0.0710** | 0.0775 |
| XGBoost quantiles v2 | 0.103 | 0.094 | 0.081 | 0.066 | 0.017 | 0.0721 | 0.0772 |
| NGBoost normal / Laplace / t, v2 | 0.105-0.107 | 0.094-0.096 | 0.083-0.084 | 0.069 | 0.019 | 0.074-0.075 | 0.075-0.079 |
| Blend v1 (baseline, NGBoost, LightGBM) | 0.103 | 0.095 | 0.080 | 0.065 | 0.017 | 0.0718 | 0.0764 |
| Blend v2 core | 0.102 | 0.093 | 0.081 | 0.065 | 0.017 | 0.0715 | 0.0775 |
| Blend v2, all six | 0.103 | 0.093 | 0.081 | 0.066 | 0.017 | 0.0719 | 0.0779 |

Blend scores are cross-fitted (each quarter of validation scored with weights fitted
on the other quarters), so they are not in-sample. Final blend weights put 0.79 to
0.85 on LightGBM v2.

**LightGBM v2 minus v1** (paired, weekly block bootstrap, 87 weeks, 5,000 resamples):
Brier 0.0011 better, 95% CI [0.0005, 0.0017], 99.96% of resamples positive. Better at
every hour (00 CT 0.0017, 06 CT 0.0023, 10 CT 0.0007, 14 CT 0.0003, 17 CT 0.0006).
**But no better on the hottest 20% of days (−0.00001) and slightly worse in June to
August (−0.0004).**

Calibration of LightGBM v2: 80% intervals cover 82.2%, 95% cover 95.4%. PIT deciles
0.07 to 0.12 (v1 similar). NGBoost normal is shifted (PIT 0.067 in the lowest decile,
0.115 in the highest), which is why it loses.

## What drives it (share of gain, median LightGBM v2 model)

| Feature | Share |
| - | -: |
| Mean of ERCOT's 8 model forecasts / in-use forecast | 15% |
| Current load / forecast for the remaining hours | 12% |
| Load ramp over the last hour | 10% |
| Last completed hour: actual / forecast | 8% |
| Season and forecast level | 14% |
| Recent bias (3, 7, 14 days) | 11% |
| All weather features together | 14% (warmest hour so far 3.2%, forecast max 2.7%, NBM spread 2.2%) |

## Reading

- Weather adds a little, mostly on mild days and early in the day. ERCOT's load
  forecast is already built on weather forecasts, so a second weather forecast
  carries little new information about the peak; live load against forecast
  carries most of it. On the hot summer days that make up most Kalshi volume,
  v2 is no better than v1.
- Other model families and blends did not help: LightGBM already gets the
  disagreement and multi-forecast signals, and the blends give it most of the
  weight.

## Choice (D-044)

Frozen for P4E: **LightGBM v2 alone** (quantile regression on 31 features with
conformal calibration on 2024, fitted on 2017-07 to 2023), because it has the best
validation Brier overall and at every decision hour, and no blend or other model beat
it at every hour (the registered rule for keeping a component). Fitted object:
`data/interim/ercot/frozen/p4e_lgbm_v2.pkl`, sha256
`816ab8652eb46a59ce38f4f2f28e63e6926461b811b939c7949df4dd3dad48d9`, from the
code at the commit that adds this file; refitting from that commit gives the same
model (fixed seeds).

Caveat for P4E: about a dozen configurations were compared on validation. The
chosen model's margin over v1 is small, so its validation score is a little
optimistic. The comparison with the market in P4E is not affected by this choice
in a way that favours the model, because the Kalshi period was never used.
