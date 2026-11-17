# pm-weather

**Is there a tradable edge in niche weather and energy prediction markets?**

A research project on Kalshi's snowfall and ERCOT (Texas grid) peak-demand
markets, built to answer that question the way a careful quant desk would: log
the market properly, replicate settlement exactly, pre-register every test, and
compare the market's prices against free public forecasts before building any
model.

Status (2026-11-16): market logger live since 2026-11-14. Two pre-registered
experiments run; in both, the market was more accurate than the free public
forecast. A Phase 4 ERCOT nowcast beats the forecast-based baseline by 20% out
of sample; whether it beats the *market* is the next pre-registered experiment
(P4E). No trading.

## Why these markets

Kalshi lists contracts such as "Will New York City get more than 4 inches of
snow in December?" or "Will ERCOT's peak demand tomorrow exceed 62,000 MW?" as
ladders of strikes, so the prices across a ladder are the market's implied
distribution for the underlying quantity. Public agencies publish calibrated
probabilistic guidance for the same quantities for free: the National Weather
Service's National Blend of Models (NBM) for snowfall, ERCOT's seven-day load
forecast for demand.

Hourly temperature markets are crowded with bots. Snowfall, storm-window and
grid-demand markets have the same structure, real volume (last season's
storm-window markets traded over 10 million contracts across six events; a
median stressed summer ERCOT day trades about 38,000 contracts), and far fewer
participants who model them.

The first question is therefore simple and falsifiable: **at the same
timestamp, is the market's probability better or worse than the free forecast?**

## Approach

1. **Log the market.** Full-depth order books, trades and rule text for every
   open snow, low-temperature, ERCOT-demand and power-price market, discovered
   automatically. Order books cannot be recovered later, so this came first.
2. **Replicate settlement.** Recompute every historical outcome from the
   official source data. A backtest is not trusted until this matches.
3. **Pre-register.** Hypotheses, sample, metrics and the go/no-go rule are
   written down and frozen before any price is compared with any forecast
   (`docs/preregistration.md`). The rule's thresholds were themselves chosen by
   simulation of its false-positive rate and power. Each experiment runs once.
4. **Test the free baseline.** Market versus public forecast versus
   climatology, scored with proper scoring rules and block-bootstrap intervals
   clustered by week (storms and heat waves move many contracts at once).
5. **Model only after a gate.** A model has to beat the public forecast out of
   sample on history before it is compared with the market, on data it never saw.
6. **Paper trade before any capital.**

## Results so far

**Settlement replication: 1,730 settled contracts recomputed from official data.**

- Snow: 396 settled markets (monthly ladders in 11 cities, New York back to
  2021, and last winter's storm-window markets) recomputed from NWS daily
  climate reports. 389 reproduce Kalshi's result and none contradict it. The
  remaining 7 belong to one Chicago event that Kalshi itself unwound.
- ERCOT: all 1,334 settled daily-peak markets reproduce from ERCOT's
  first-complete "Actual System Load by Forecast Zone" report.

**P2A, snow: the market beat the NWS forecast.** Across 16,268 matched
observations (382 markets, 2021 to 2026), Kalshi's prices were more accurate
than a probability built from NBM snowfall percentiles plus observed snow and
climatology: Brier 0.148 against 0.158, difference D = -0.010 with a 95%
block-bootstrap interval of [-0.017, -0.003]. A naive taker trading NBM against
the quote made +$508 on 33,653 contracts after fees, indistinguishable from
zero. Verdict under the registered rule: CONDITIONAL.
[Write-up](docs/findings/2026-11-15-p2a-result.md).

![Reliability diagram: Kalshi market, NBM composite and climatology](docs/findings/figures/p2a_reliability.png)

**P3A, ERCOT: the market beat ERCOT's own load forecast.** 1,551 observations
(785 markets, 76 operating days, Aug to Nov 2026): Brier 0.056 against 0.067,
D = -0.011, 95% interval [-0.018, -0.004]; every registered sensitivity variant
has the same sign. The market's advantage is largest late in the day, when it
sees real-time load that a day-ahead forecast does not contain. A naive taker
made +$261 after fees, but the more it disagreed with the market the worse it
did (perceived-versus-realized edge correlation -0.33), so that P&L is not
evidence of edge. Verdict: CONDITIONAL.
[Write-up](docs/findings/2026-11-16-p3a-result.md).

**Phase 4, ERCOT nowcast v1 (validation, no prices).** An intraday model of the
daily peak, trained on nine years of 5-minute grid telemetry and every archived
forecast posting, with a strict no-lookahead check. On held-out 2025-26 data the
LightGBM-plus-conformal model scores Brier 0.072 against 0.090 for the P3A
forecast-based method (20% better; 7% at midnight, 29% at 2 pm, 65% at 5 pm),
and its 80% and 95% intervals cover 81.5% and 95.5% of outcomes. This is not yet
an edge over the market, which beat the same baseline in P3A.
[Write-up](docs/findings/2026-11-16-phase4-ercot-v1.md).

**What this adds up to.** Free public guidance is priced into both markets. Any
edge has to come from information or modelling the crowd does not already use,
and has to be shown on data the model never saw, against real bid and ask
prices after fees. The samples are small and clustered (about 20 effective
weeks of snow history, 12 calendar weeks of ERCOT), so every interval is
reported with its block structure.

## Mathematical and statistical methods

Each method below is implemented in the module named next to it, and each of
those modules has tests in `tests/`.

### Contract pricing and execution costs

- **Market-implied probability.** A binary contract paying $1 if $X > K$ trades
  at a price that, net of spread, is the market's $P(X > K)$; a strike ladder is
  a discretised survival function. Prices are scored at the bid-ask mid, and the
  cost of using the mid instead of the unknown belief inside a spread of width
  $s$ is bounded by $s^2/12$ in Brier score (uniform belief within the spread),
  under 0.001 at the registered spread filter. (`docs/findings/2026-11-14-prereg-design.md`)
- **Exchange fee model.** Taker fee $= \lceil 0.07 \cdot C \cdot P(1-P) \rceil$
  rounded up to $10^{-6}$, then the balance change floored to $10^{-4}$; exact
  decimal arithmetic, tested against the exchange's worked example. The fee is
  quadratic in price and peaks at $P = 0.5$. (`src/pmw/markets/fees.py`)
- **After-fee expected value per contract.** Buying YES:
  $\mathbb{E} = p - \text{ask} - \text{fee}(\text{ask})/C$; buying NO:
  $(1-p) - (1-\text{bid}) - \text{fee}(1-\text{bid})/C$. Every edge number in
  this project is measured against the quoted side, not the mid.
- **Capacity-aware trade simulation.** A registered naive taker trades when
  after-fee edge exceeds a threshold $\tau$, sized at
  $\min(10\%\ \text{of hourly volume}, 250)$ contracts per market-day with a
  1,000-contract cap per event-day, ranking candidates by edge.
  (`src/pmw/eval/p2a_score.py`)

### Probabilistic forecasting models

- **Snow: Monte Carlo composite distribution.** At decision time $t$,
  $\text{Total} = \text{Observed} + \text{Forecast} + \text{Climate}$, and
  $P(\text{Total} > K)$ is estimated by simulation. Each NBM 24-hour period is a
  piecewise-linear quantile function through its 10/25/50/75/90th percentiles
  with a point mass at zero and an exponential upper tail; consecutive periods
  are joined by a **Gaussian copula** (Spearman rank correlation 0.5, 0 as a
  registered sensitivity); days beyond the forecast horizon come from
  **whole-season block resampling** of station climatology so multi-day storms
  keep their structure. "Strictly greater than $K$" on data reported to 0.1 in
  becomes a continuity correction at $K + 0.05$. (`src/pmw/models/nbm_composite.py`)
- **ERCOT baseline: empirical error-ratio distribution.** With $F$ the forecast
  daily peak and historical ratios $r_i = A_i / F_i$ from the same posting slot
  and a $\pm 30$-day seasonal window,
  $P(A > K) = \frac{1}{n}\sum_i \mathbf{1}[F r_i > K]$. (`src/pmw/eval/p3a_score.py`)
- **ERCOT nowcast: structural target decomposition.** The settled peak is
  $A = \max(H, R)$, with $H$ the peak already observed in real-time telemetry and
  $R$ the peak still to come. Only $R$ is modelled, as
  $y = \log(R / F_{\text{rem}})$, and
  $P(A > K) = 1 - P(H \le K)\, P(R \le K)$, where the telemetry-to-settlement
  gap in $H$ is resampled from history. (`src/pmw/models/ercot_peak.py`)
- **NGBoost** (natural-gradient boosting) fitting a Normal distribution to $y$,
  with a post-hoc variance recalibration
  $\sigma \leftarrow \sigma \sqrt{\overline{z^2}}$ on a held-out calibration year.
- **LightGBM quantile regression** (pinball loss) at 23 quantile levels, with a
  **split-conformal** shift per level on the calibration year (CQR-style),
  sorting to remove quantile crossing, and CDF interpolation to price any strike.
- **Time-ordered splits.** Train to 2023, calibrate 2024, validate 2025 to
  Aug 2026; the Kalshi test period is untouched. Weather data are split by
  season, never at random, and every feature row passes an automated check that
  its inputs were published before the decision time.

### Evaluation and inference

- **Proper scoring rules:** Brier score and log loss (probabilities clipped to
  [0.01, 0.99]); the primary metric is the paired difference
  $D = \overline{\text{BS}_{\text{market}} - \text{BS}_{\text{forecast}}}$.
- **Murphy decomposition** $\text{BS} = \text{REL} - \text{RES} + \text{UNC}$ and
  reliability diagrams, separating calibration from discrimination (in P3A both
  were calibrated; the market had higher resolution).
- **Block bootstrap** (10,000 seeded resamples of whole calendar weeks) for
  intervals on $D$ and on strategy P&L, because outcomes of one storm or heat
  wave are strongly correlated across cities and strikes.
- **Kish effective sample size** $n_{\text{eff}} = (\sum_w n_w)^2 / \sum_w n_w^2$
  over weekly blocks, which shows that hundreds of contracts are only about 20
  independent weeks.
- **Perceived-versus-realized edge correlation** as a diagnostic for whether
  trading P&L comes from the model's signal or from luck.
- **Design of the decision rule by simulation.** Before any result was seen,
  synthetic worlds with known truth (Bernoulli outcomes, logit-noise
  forecasters, shared weekly shocks, heavy-tailed week effects) measured the
  false-positive rate and power of candidate go/no-go thresholds. The P&L
  threshold (positive in 90% of resamples) was then chosen by minimising
  expected decision cost under stated priors.

## What is built

| Piece | Where | Notes |
| - | - | - |
| Kalshi public-data client | `src/pmw/markets/kalshi.py` | Paced, retrying, batching, live and historical endpoints. Holds no credentials and has no order code. |
| Fee model | `src/pmw/markets/fees.py` | Exact decimal arithmetic, tested against the exchange's documented example. |
| Market discovery | `src/pmw/logger/discovery.py` | Regex filters over series metadata, no hardcoded tickers. |
| Market logger | `src/pmw/logger/` | Deployed on Railway. 5-minute books for everything, 1-minute for storm markets and volume spikes, append-only Parquet, gap detection, daily report. |
| Snow settlement replication | `src/pmw/settle/snow.py` | Period, station and strike read from each market's rule text; trace, strict inequality and missing-day rules from the contract terms. |
| ERCOT settlement replication | `src/pmw/settle/ercot.py` | First complete version of each day's load report, from ERCOT's posting archive. |
| NWS and NBM data | `src/pmw/weather/` | Daily snowfall from the NWS climate report (via Iowa Environmental Mesonet), ACIS as a second source, NBM text bulletins parsed per cycle. |
| ERCOT data | `src/pmw/energy/` | API client, archive backfill (2017 on) of actual load, seven-day forecasts and 5-minute real-time telemetry, run from a US cloud region. |
| Models | `src/pmw/models/` | Snow Monte Carlo composite; ERCOT baseline, NGBoost and LightGBM-conformal nowcasts. |
| Experiments | `src/pmw/eval/` | P2A and P3A scoring, block bootstrap, Murphy decomposition, taker simulation, synthetic-world tests. |
| Time handling | `src/pmw/utils/timeutil.py` | NWS climate days run on local standard time all year; ERCOT operating days in Central time with DST flags. |

About 8,200 lines of source and 166 tests, which run offline on saved real API
responses. `make check` runs ruff, mypy and pytest.

```
make setup                     # Python 3.12 environment via uv
make check                     # lint, types, tests
make discover                  # list every open market that matches the filters
make log-test                  # run the logger for three quick cycles
make settle-check SERIES=all   # replicate every settled snow market
uv run pmw ercot-settle-check  # replicate every settled ERCOT daily-peak market
make p2a                       # experiment P2A (snow)
uv run pmw p3a                 # experiment P3A (ERCOT)
uv run pmw ercot-model         # Phase 4 ERCOT nowcast, validation report
```

## Standards this project holds itself to

- No lookahead: every forecast joined to a price must have been published
  before it, and this is checked automatically.
- Walk-forward by season; the final test period is touched once.
- Three baselines in every comparison: climatology, the public forecast, and
  the market itself.
- Uncertainty from block bootstrap clustered by week, never from treating
  correlated strikes as independent bets.
- Edge is measured against the quoted bid or ask after fees, with the size the
  book could actually have filled.
- Experiments are pre-registered and run once; deviations are logged.
- Negative results get written up.

## Roadmap

| Phase | Question | Status |
| - | - | - |
| 0 | Scaffold, environment, conventions | done |
| 1 | Live market logging | running since 2026-11-14 |
| 2 | Do snow prices beat NBM guidance? | done: no, the market is more accurate (CONDITIONAL) |
| 3 | Do ERCOT prices beat ERCOT's own forecast? | done: no, the market is more accurate (CONDITIONAL) |
| 4 | Distributional snowfall and load models | in progress: ERCOT nowcast v1 validated; P4E (model vs market) next |
| 5 | Paper trading, then small live with hard limits | only on explicit approval |

## Documents

- [`docs/preregistration.md`](docs/preregistration.md): hypotheses and decision rules
- [`docs/findings/`](docs/findings/): every result, including the design analysis behind the rules
- [`docs/phase4_ercot_plan.md`](docs/phase4_ercot_plan.md): the ERCOT modelling plan
- [`docs/settlement_rules.md`](docs/settlement_rules.md): rule text and interpretation per series
- [`docs/data_sources.md`](docs/data_sources.md): every source, with quirks
- [`docs/decisions.md`](docs/decisions.md): dated log of design decisions
- [`docs/deploy.md`](docs/deploy.md): hosting

This is research code. It places no orders and is not financial advice.
