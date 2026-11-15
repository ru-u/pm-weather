# pm-weather

**Is there a tradable edge in niche weather and energy prediction markets?**

A research project on Kalshi's snowfall and ERCOT electricity-demand markets,
built to answer that question the way a careful quant desk would: log the
market properly, replicate settlement exactly, pre-register the test, and
compare the market's prices against free public forecasts before building any
model.

Status (2026-11-15): market logger live; first pre-registered experiment run
(the market beat the free forecast). No model, no trading.

## Why these markets

Kalshi lists contracts such as "Will New York City get more than 4 inches of
snow in December?" as ladders of strikes, so the prices across a ladder are the
market's implied distribution for the snowfall total. The National Weather
Service publishes its own calibrated probabilistic snowfall guidance (the
National Blend of Models) for the same stations, for free, every hour.

Hourly temperature markets are crowded with bots. Snowfall, storm-window and
grid-demand markets have the same structure, real volume in winter (last
season's storm-window markets traded over 10 million contracts across six
events), and far fewer participants who model them.

The first question is therefore simple and falsifiable: **at the same
timestamp, is the market's probability better or worse than the free forecast?**

## Approach

1. **Log the market.** Full-depth order books, trades and rule text for every
   open snow, low-temperature, ERCOT-demand and power-price market, discovered
   automatically. Order books cannot be recovered later, so this came first.
2. **Replicate settlement.** Recompute every historical outcome from the
   official source data. A backtest is not trusted until this matches.
3. **Pre-register.** Hypotheses and the go/no-go rule are written down before
   any price is compared with any forecast (`docs/preregistration.md`).
4. **Test the free baseline.** Market versus NBM versus climatology, scored
   with proper scoring rules and block-bootstrap intervals clustered by storm.
5. **Model only if a gate passes.** A distributional snowfall model has to beat
   NBM out of sample before it is compared with the market.
6. **Paper trade before any capital.**

## Results so far

- **Settlement replication:** all 396 settled snowfall markets (monthly city
  ladders in 11 cities, New York back to 2021, and last winter's storm-window
  markets) were recomputed from official NWS daily snowfall. 389 reproduce
  Kalshi's result and none contradict it. The remaining 7 belong to one Chicago
  event that Kalshi itself unwound, and are excluded from analysis.
- **First pre-registered test: the market beat the free forecast.** Across 16,268
  matched observations (382 markets, 2021 to 2026), Kalshi's snow prices were
  more accurate than a probability built from the National Blend of Models plus
  observed snow and climatology: Brier score 0.148 against 0.158, difference
  -0.010 with a 95% block-bootstrap interval of [-0.017, -0.003]. A naive
  strategy trading NBM against the quote made +$508 on 33,653 contracts after
  fees, statistically indistinguishable from zero. Free guidance is priced in.
  Under the registered rule the result is CONDITIONAL: any snow model must first
  beat NBM out of sample.
- **The sample is small.** Kalshi has roughly 62 settled city-months and 6
  storm windows of snow history, heavily clustered. One winter of market data
  cannot prove a small edge, so the forecast itself is validated separately on
  hundreds of stations and six winters where the sample is large.
- **Rules moved.** This season's contracts name a commercial data provider ahead
  of the NWS in the settlement hierarchy. The logger versions every market's
  rule text for exactly this reason.

Details: [`docs/findings/`](docs/findings/), especially the [P2A result](docs/findings/2026-11-15-p2a-result.md).

## What is built

| Piece | Where | Notes |
| - | - | - |
| Kalshi public-data client | `src/pmw/markets/kalshi.py` | Paced, retrying, batching. Holds no credentials and has no order code. |
| Fee model | `src/pmw/markets/fees.py` | Exact decimal arithmetic, tested against the exchange's documented example. |
| Market discovery | `src/pmw/logger/discovery.py` | Regex filters over series metadata, no hardcoded tickers. |
| Market logger | `src/pmw/logger/` | 5-minute books for everything, 1-minute for storm markets and volume spikes, append-only Parquet, gap detection, daily report. |
| Settlement replication | `src/pmw/settle/snow.py` | Period, station and strike read from each market's rule text; trace, strict inequality and missing-day rules from the contract terms. |
| NWS climate data | `src/pmw/weather/` | Daily snowfall per station from the Daily Climate Report (via Iowa Environmental Mesonet), with ACIS as a second source for missing days. |
| Time handling | `src/pmw/utils/timeutil.py` | NWS climate days run on local standard time all year. |

```
make setup      # Python 3.12 environment via uv
make check      # lint, types, tests
make discover   # list every open market that matches the filters
make log-test   # run the logger for three quick cycles
make settle-check SERIES=KXNYCSNOWM
```

## Standards this project holds itself to

- No lookahead: every forecast joined to a price must have been available before it.
- Walk-forward by season; the final test season is touched once.
- Three baselines in every comparison: climatology, NBM, and the market itself.
- Uncertainty from block bootstrap clustered by storm and date, never from
  treating correlated strikes as independent bets.
- Edge is measured against the quoted bid or ask after fees, with the size the
  book could actually have filled.
- Negative results get written up.

## Roadmap

| Phase | Question | Status |
| - | - | - |
| 0 | Scaffold, environment, conventions | done |
| 1 | Live market logging | running since 2026-11-14 |
| 2 | Do snow prices beat NBM guidance? | done: no, the market is more accurate (CONDITIONAL) |
| 3 | Do ERCOT demand prices beat ERCOT's own forecast? | not started |
| 4 | Distributional snowfall and load models | only if a gate passes |
| 5 | Paper trading, then small live with hard limits | only on explicit approval |

## Documents

- [`docs/phase2_plan.md`](docs/phase2_plan.md): the next experiment
- [`docs/preregistration.md`](docs/preregistration.md): hypotheses and decision rules
- [`docs/settlement_rules.md`](docs/settlement_rules.md): rule text and interpretation per series
- [`docs/data_sources.md`](docs/data_sources.md): every source, with quirks
- [`docs/decisions.md`](docs/decisions.md): dated log of design decisions
- [`docs/deploy.md`](docs/deploy.md): hosting

This is research code. It places no orders and is not financial advice.
