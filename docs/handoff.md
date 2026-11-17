# Handoff: start of the next session (written 2026-11-16)

Paste this into a new Claude Code session opened in this folder, or tell it
"Read docs/handoff.md and start." `CLAUDE.md` loads by itself; this file adds
only what changed since it was last updated, plus the work queue.

---

## Prompt

Continue the pm-weather project. Read `CLAUDE.md`, then
`docs/findings/2026-11-16-phase4-ercot-v1.md`, `docs/phase4_ercot_plan.md`,
decisions D-040 to D-042 in `docs/decisions.md`, and the P3A section of
`docs/preregistration.md`. Then do the work queue below in order. Report after
each chunk (what was built, what was verified, what is next) in plain language
with a small table where numbers matter, and commit after each meaningful step
(`make check` must pass first).

### Where things stand

- **Phase 4 is open for ERCOT and snow (D-040).** The owner chose to build
  models after P2A and P3A came out CONDITIONAL, following the write-up of a
  profitable Kalshi temperature strategy (NGBoost on live observations plus
  forecasts, log-odds blend, trading naive opening quotes). The "documented
  forecast weakness" clause is overridden; the rest of the discipline
  (train/validate/held-out, pre-register, run once) stands. `CLAUDE.md`'s
  Phases section does not say this yet: update it first (small edit).
- **ERCOT nowcast v1 (D-041), `src/pmw/models/ercot_peak.py`,
  `uv run pmw ercot-model [--rebuild]`.** Target A = max(H, R): H is the peak
  already observed (NP3-910-ER telemetry), R the remaining-hours peak, modeled
  as y = log(R / F_rem). Decisions at 00/06/10/14/17 CT, using the 23:30/05:30/
  09:30/13:30/16:30 postings. Train to 2023-12-31, calibrate 2024, validate
  2025-01-01 to 2026-08-27 (3,015 rows). Validation Brier on a Kalshi-like grid
  (17 strikes, 500 MW apart, ±4 GW around F): P3A baseline 0.090, NGBoost
  0.074, LightGBM quantiles + conformal 0.072 (best at every hour; 17 CT 0.051
  to 0.018). LightGBM coverage 81.5% / 95.5%. Leakage check 0 rows.
- **The Kalshi period (2026-08-28 to 2026-11-14) has not been touched by any
  Phase 4 choice.** Keep it that way until P4E is frozen.
- **The owner's preferences:**
  - Do not add "no live orders" warnings. The owner will approve live trading
    in writing once the system is ready. The hard rule still applies; just
    don't repeat it.
  - No temperature markets ("too competitive, the regime changed"). Work only
    on ERCOT and snow.
  - Plain-language reports.
  - The owner likes the findings artifact (dark, Raycast-style, background
    blue near snow and red near ERCOT):
    https://claude.ai/artifact/XwzdTbTD9jJSdDHki145SP. Update that URL, not a
    new one, if asked. Its source file was in a session scratchpad and may be
    gone: read the artifact back first.

### Data on disk (laptop, gitignored)

| Path | Contents | Coverage |
| - | - | - |
| `data/interim/ercot/daily_peak.parquet` | first-complete NP6-346 daily peak (settlement value) | 2017-06-29 to 2026-11-14 |
| `data/interim/ercot/hourly_load.parquet` | settled hourly load | same |
| `data/interim/ercot/forecast_peaks.parquet`, `forecast_hourly.parquet` | NP3-565 postings, 8 models + INUSE (in-use flag is per HOUR) | 2017-06-29 on |
| `data/interim/ercot/telemetry_5min.parquet`, `telemetry_hourly.parquet` | NP3-910-ER real-time load (15-min early, 5-min later) | 2017-05-30 to 2026-11-13 |
| `data/interim/ercot/peak_dataset.parquet`, `peak_model_report.json` | v1 modeling table and results | |
| `data/raw/kalshi/p3a/` | 1,335 settled KXTXERCOTPEAKD markets with hourly candles (bid/ask) | Aug 28 to Nov 14 2026 |
| `data/interim/nbm/`, `data/raw/nbm/` | NBP/NBS bulletins for the SNOW stations only | |

Raw ERCOT zips are in the Railway bucket under `raw/ercot/<emil>/...`. Pull
with `make pull-data PREFIX=raw/ercot/<emil>`. This runs through
`railway run --service logger` because `.env` has no PMW_S3 variables.
Rebuild with `uv run pmw ercot-build`.

### Gotchas already paid for

- LightGBM and XGBoost need `brew install libomp` (already done on this laptop).
- NP3-910-ER files before 2018 are named `48h_*`, after that `2d_*`. Both
  parsers accept both. Early data is 15-minute, so `MIN_POINTS_PER_HOUR=3`.
- Kalshi candles: the live endpoint returns 404 for markets that have moved
  to the historical tier. Fall back to `historical_candlesticks` (done in
  `eval/p3a.py`).
- The Railway bucket returns SLOW_DOWN under load. Wrap bucket writes in
  `energy.jobs.with_retry`.
- ERCOT calls run only on a deployed Railway replica in a US region
  (`require_us_railway`); `railway run` executes on the laptop and fails the
  guard. Deploy jobs with `railway up --service ercot-jobs --ci`. Set its
  start command or cron schedule through the GraphQL `serviceInstanceUpdate`
  mutation, as in `docs/deploy.md`. `ercot-jobs` is the only service holding
  ERCOT credentials. Never move them to `logger` or `daily-report`.
- The Railway account is shared with other projects. Keep jobs short and
  lean; the overage budget is about $10 a month in total.

---

## Work queue

### 1. ERCOT v2: weather features and a blend (validation only)

- **Hourly temperature forecasts** for KIAH, KDFW, KAUS and KSAT, about 2021
  on.
  - `pmw nbm-pull` reads its stations from `configs/stations.yaml`, which
    lists only snow stations. Add a `--stations` option, or a separate
    `configs/ercot_stations.yaml`. Don't put Texas stations into the snow
    config.
  - NBS gives 3-hourly temperature to about 72 h; NBH is hourly to about
    25 h. Check which products and cycles exist in the AWS archive for those
    years. Add NBH to `nbm_text.PRODUCTS` if it is worth it, with a parser
    test on a saved real bulletin.
  - Join on `available_at < decision time`, never on cycle time.
- **Observed temperature so far today** from IEM ASOS (hourly METAR) for the
  same four stations. Add a fixture-tested parser.
- **Candidate features:** a load-weighted Texas temperature (weights from zone
  shares) and its forecast maximum for the remaining hours; observed minus
  forecast temperature so far (the same disagreement idea as load); cooling and
  heating degree hours.
- **Blend** Baseline, NGBoost and LightGBM in log-odds per strike, with weights
  fitted on validation. Also consider a small set of models that differ
  meaningfully: NGBoost with a different distribution (for example Student-t
  or LogNormal on R/F_rem), and XGBoost quantiles. Keep only what improves
  validation Brier at every decision hour.
- Report the v1→v2 table by hour and on the hottest 20% of days, coverage,
  PIT, and feature importance. Write it up in a findings doc and a D-043 (D-042 is the incentives note).
- Choose ONE final model configuration on validation and freeze it, with its
  git hash.

### 2. Pre-register P4E, then run it once

Write P4E in `docs/preregistration.md` before joining any price.
- **Question:** at 00/06/10/14/17 CT on 2026-08-28 to 2026-11-14, is the
  frozen model a better probability forecast than the Kalshi mid, and does
  taking against bid/ask make money?
- **Metrics:**
  - Brier difference D with a weekly block bootstrap, as in P3A.
  - Naive taker P&L at a threshold set in advance, with the Kalshi fee
    formula.
  - Correlation between perceived edge (model − price) and realized edge,
    as a headline metric. It was −0.33 in P3A: the write-up's failure mode.
  - Capacity from book depth.
- Use `eval/p3a_score.py` for the bootstrap, decision and capacity helpers.
  The candles are already on disk.
- Record the caveat: this period was P3A's test sample, so P3A's result
  (market beat ERCOT's forecast) is already known. Model choices must be
  shown to come only from validation, by git history and dates.
- Freeze it (timestamp in UTC), run once, report, record a decision (D-044).
  Do not retune afterwards. Anything further is a new registered experiment.

### 3. Live inputs for paper trading (ERCOT)

The model needs at decision time the same inputs it was trained on.
- Real-time system load: ERCOT's public dashboard JSON. Check the endpoint,
  its terms and whether it is geo-blocked. If it is, run it from Railway.
- Each hourly NP3-565 posting as it appears.
- The latest NBM bulletin for the four Texas stations.
- Kalshi `GET /incentive_programs` (public, no auth) once a day, so we know
  when a series has a liquidity reward (D-042). This one can run in
  `daily-report`: it needs no ERCOT credentials.

Build this as a small cron on Railway that writes snapshots to the bucket. Run
it from `ercot-jobs` or from a new service; whichever service makes ERCOT
calls must hold the ERCOT credentials, and the logger must not. Estimate the
cost in `docs/deploy.md` before deploying. Ask the owner before adding any new
service that holds ERCOT credentials.

Then register P3A-S / P4-paper (a prospective test on live logged books).
Note the write-up's execution edge: opening quotes at 23:00 CT (median
spread 0.82) and stale quotes after each :30 posting.

### 4. Snow (after ERCOT step 2)

- **Run P2B** (NBM calibration against CLI snowfall). It is registered as a
  DRAFT in `docs/preregistration.md`: freeze it first, then run it.
- Plan a storm-window nowcast in the same shape as ERCOT. Inputs:
  - observed snowfall so far (CLI and ASOS);
  - NBM percentiles;
  - HRRR snowfall and QPF (via `herbie`);
  - a snow-to-liquid ratio model;
  - a zero-inflated or LogNormal NGBoost.
  Test it prospectively on live books (P2A-S). Write the plan as
  `docs/phase4_snow_plan.md` before building.

### Small, dated items

- **On or after 2026-11-22:** re-check KXSNOWCITIES-26NOV15 once it has
  expired:
  `uv run pmw settle-check KXSNOWCITIES --include-open`
  It should reproduce, and the "Above" rule should be applied as strictly
  greater.
- **After any change to `settle/` or `weather/`:**
  `uv run pmw settle-check all --brief` must still show 389 reproduced and
  0 contradicted.
- **After any change to ERCOT parsing:** `uv run pmw ercot-settle-check` must
  still show 1,334 of 1,334.
- **Logger health:** run `make report` for yesterday, and check Railway usage
  occasionally.
