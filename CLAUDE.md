# pm-weather (`pmw`): working notes for Claude Code

Solo quant research project. Question: is there a tradable edge in niche weather
and energy prediction markets on Kalshi (snowfall first, ERCOT peak demand
second), measured to the standard of a serious research writeup? The repo also
serves as the owner's main quant resume project, so `README.md` must stay
presentable and every claim must be reproducible.

The full kickoff brief is summarised here. Where the brief and what we found on
2026-11-14 disagree, this file and `docs/decisions.md` carry the corrected facts.

## Hard rules

1. **Never place a live order.** Paper trading only until the owner approves
   live trading in writing in `docs/decisions.md`.
2. **Loggers never hold trading credentials.** `pmw log`, `pmw discover`,
   `pmw report` and all research code use Kalshi's public REST endpoints.
   `src/pmw/markets/kalshi.py` has no signing code on purpose. Do not add any.
   Authenticated trading goes in a separate module used only by a separate
   trading service.
3. **Never commit, print, log or echo a secret.** Secrets live in `.env`
   (gitignored). `keys.txt` is the owner's original key file, also gitignored.
   Check shape or presence of a secret without printing its value.
4. **No lookahead.** Any feature joined to a market timestamp needs
   `issue_time < market_ts`. Split weather data by season, never at random.
5. **Pre-register.** Write hypotheses and the decision rule in
   `docs/preregistration.md` before looking at results. Log deviations.
6. **Settlement replication gates everything.** A backtest is not trusted until
   `src/pmw/settle/` reproduces every historical settlement in its sample, or
   each mismatch is explained in `docs/settlement_rules.md`.
7. **Phases in order.** Do not start modeling or trading work before its
   go/no-go gate passes (see Phases).
8. **ERCOT API calls run from Railway (US region), not the laptop.**
9. Respect rate limits and terms of service. No market manipulation.
10. When official docs and the brief disagree, trust the docs and tell the owner.

## Working style

- Small, reviewed steps. After each chunk: what was built, what was verified,
  what is next.
- Ask the owner only when blocked or when a choice is expensive to undo.
  Otherwise choose, record it in `docs/decisions.md` (dated, numbered `D-0xx`),
  and continue.
- Tests for parsers (on saved real responses in `tests/fixtures/`), settlement,
  fees and time handling, written alongside the code.
- `make check` (ruff, mypy, pytest) must pass before a commit.

## Commands

```
make setup          # uv sync --extra research + pre-commit
make check          # lint + typecheck + test
make discover       # every open Kalshi market matching configs/series.yaml
make log            # run the market logger (local ./data unless PMW_S3_* set)
make log-test       # 3 quick cycles for a smoke test
make report         # daily logger report (yesterday UTC)
make settle-check SERIES=all        # or one series, e.g. KXNYCSNOWM
make nbm nbs climo  # pull and parse NBM bulletins and station climatology (resumable)
make p2a            # experiment P2A: observed table, forecasts (no prices), scoring
make live-snow-inputs   # 2026-27 composite inputs (no prices) into data/interim/live_snow
make p2as-health    # P2A-S storm-window data health (counts only); scoring waits for 2027-05-15
uv run pmw plugins                  # strategies and models available, params in use
uv run pmw backtest OBS.parquet     # paper taker backtest (naive baseline unless a plugin is named)
make pull-data      # copy the Railway bucket to ./data/bucket (PREFIX=raw/ercot for part)
uv run pmw ercot-build              # ERCOT tables from the pulled archive
uv run pmw ercot-settle-check       # replicate every settled KXTXERCOTPEAKD market
```

Environment: Python 3.12 via `uv`; versions pinned in `uv.lock`. Core
dependencies are only what the logger needs. Heavy stacks are extras:
`research` (duckdb, pandas, scipy, ...), `modeling`, `weather` (herbie, xarray,
cfgrib), `energy` (gridstatus). `uv sync --all-extras` installs everything.

## Layout

```
configs/      series.yaml (discovery filters), stations.yaml, logger.yaml, params.example.yaml
docs/         decisions, preregistration, commitments, settlement_rules, data_sources, deploy, findings/
src/pmw/
  markets/    kalshi.py (public client), fees.py
  weather/    iem_cli.py, cli_text.py (NWS CLI), acis.py, asos.py, nbm_text.py, hrrr_zarr.py
  energy/     ercot.py (API client, parsers), jobs.py (Railway probe/backfill), live.py, build.py
  logger/     discovery, schemas, buffer, runner, report
  settle/     snow.py (SNOWOVERTIME), ercot.py (KXTXERCOTPEAKD)
  features/   nbm_table.py, obs_table.py, ercot_weather.py
  models/     nbm_composite.py (observed + NBM + climatology), ercot_peak.py, ercot_peak_v2.py
  eval/       p2a.py, p2a_score.py, p2b.py, p3a.py, p3a_score.py
  plugins.py  Strategy / ProbabilityModel protocols, entry-point discovery, params
  strategy.py NaiveTaker (the registered P2A/P3A baseline)
  backtest/   taker.py (fee-aware book walk, run, settle; paper only)
  utils/      timeutil, storage, config
scripts/      pull_data.py, prereg_design.py, views.sql (DuckDB view layer)
tests/        offline; fixtures are real saved responses
data/         gitignored: raw/ interim/ processed/ live/
```

## Public and private code (D-053)

This repository is public. The tuned models, execution rules and Phase 4
preregistration texts live in a separate private package that depends on this
one and plugs in through the `pmw.strategies` / `pmw.models` entry points.

- Never import the private package here, and never copy its code, params,
  thresholds or results into this repo (docs, commit messages, tests included).
- New modelling or execution work that could reveal where an edge is goes in
  the private package. Data collection, settlement, generic features, baselines
  and methodology stay here.
- Private texts are committed here by sha256 in `docs/commitments.md` and
  revealed after their evaluation.
- Do not push tags or branches other than `main` without checking what they
  contain.

## Facts that are easy to get wrong

**Kalshi API**
- Base URL `https://external-api.kalshi.com/trade-api/v2` (recommended by the
  docs). `api.elections.kalshi.com` still works.
- Market-data REST is public. The WebSocket requires a signed handshake, so the
  logger polls REST and does not use the WebSocket.
- Books return bids only, ascending, best bid last. YES ask = 1 - best NO bid.
- Prices are dollar strings with 4 decimals, sizes are fractional contracts
  (`"7.40"`).
- `GET /markets/orderbooks` takes at most 100 tickers, as repeated `tickers=`
  parameters. `GET /markets?tickers=` takes a comma-separated list.
- Unauthenticated limit is roughly a few requests per second per IP; we pace at
  2/s and back off on 429.
- Settled markets older than the cutoff (`GET /historical/cutoff`) move to
  `/historical/*` endpoints. Use `KalshiPublic.settled_markets()`.
- ERCOT demand is under category Economics and power prices under Commodities,
  not under Climate and Weather.
- Orders (checked 2026-11-17, D-056): writes go to V2 `/portfolio/events/orders`;
  the legacy `/portfolio/orders` writes return 410. V2 quotes the YES leg only:
  `side` bid = buy YES, ask = sell YES (= buy NO at 1 - price); `price` and
  `count` are strings. Fills report `outcome_side` and `fee_cost` (dollars).
  Valid prices come from each market's `price_ranges`.
- Demo exchange: `https://external-api.demo.kalshi.co/trade-api/v2`. Demo and
  production keys each work only on their own exchange.

**Fees**
- Taker fee = 0.07 x multiplier x contracts x P x (1 - P), rounded up to
  $0.000001, then a rounding fee floors the balance change to $0.0001. All
  weather and energy series we track are `quadratic`, multiplier 1, no maker fee.
- The owner's Kalshi key is Ed25519 (PKCS#8 body stored without PEM header
  lines). Ed25519 signs the pre-sign text directly; RSA-PSS applies to RSA keys only.
- The 0.07 was confirmed from the API docs' worked example and then on a real
  fill (2026-11-19, D-057): 1 YES at $0.70, `fee_cost` $0.0147 exactly as
  `pmw.markets.fees` computes it (no cent rounding). Maker fees are unchecked.

**Settlement**
- Snow contracts (rulebook SNOWOVERTIME): source hierarchy is The Weather
  Company, then NWS, then OnTheSnow for ski areas. Last season's rules cited
  the NWS CLI report directly. "Above" is strictly greater. Trace is above 0.00
  but not above any positive strike. Missing data at the last expiration
  resolves every strike to No.
- Replication status: 389 of 396 settled snow markets reproduce, 0 contradict;
  the other 7 are the excluded `KXCHISNOWM-25DEC` event. Check with
  `uv run pmw settle-check all --brief` after any change to `settle/` or
  `weather/`. Daily snowfall comes from the CLI report (IEM); ACIS fills only
  the days the CLI lacks. Period, station and strike are parsed from rule text.
- NWS climate days run on local STANDARD time all year
  (`pmw.utils.timeutil.climate_day_bounds_utc`). Some storm-window markets say
  "local calendar days in Eastern Time" instead: read each market's rules.
- Stations come from the rules, never assumed. NYC is Central Park (KNYC).
- ERCOT daily peak: highest value in the TOTAL column of the first complete
  "Actual System Load by Forecast Zone" report (NP6-346-CD) for the operating
  day. Hourly averages, not 5-minute readings. The event ticker's date is the
  day AFTER the operating day (`KXTXERCOTPEAKD-26NOV15` is about Nov 14).
- `KXTXERCOTPEAK` (calendar-year peak) is a different contract: it settles on
  ERCOT's official SETTLED peak, not the first report. Context only.
- NWS publishes an intraday CLI around 4-5 pm local; IEM serves it until the
  final report. A CLI value issued before its climate day ended is not final
  (`pmw.settle.snow.final_only`, D-032).

**ERCOT API**
- Client `src/pmw/energy/ercot.py`: B2C ROPC id token + primary
  `Ocp-Apim-Subscription-Key`, paced at 24 requests/minute. It refuses to run
  unless `RAILWAY_REPLICA_REGION` starts with `us-` (a deployed Railway
  replica). `railway run` executes on the laptop and does not qualify.
- Archives (every posted version, `docId` + `postDatetime` in Central time) go
  back to June 2017 for NP6-346-CD (actuals) and NP3-565-CD (forecasts).
- Real CSV layouts: NP6-346-CD `OperDay,HourEnding,NORTH,SOUTH,WEST,HOUSTON,
  TOTAL,DSTFlag`; NP3-565-CD `DeliveryDate,HourEnding,<8 weather zones>,
  SystemTotal,Model,InUseFlag,DSTFlag`, 8 models x 192 hours per posting.
- Kalshi daily ERCOT markets open 23:00 CT the evening before the operating day
  and close 23:55 CT on it; Kalshi settles 7 to 12.5 hours after close.

**NBM**
- Station text bulletins are archived per cycle on AWS; `pmw nbm-pull` and
  `pmw nbm-parse` fetch and parse them. Only the 01/07/13/19Z cycles carry the
  24-hour snow percentiles (`S24P1/2/5/7/9`, tenths of an inch, periods ending
  12Z). Join on `available_at`, never on the cycle time.

**Statistics**
- Snow outcomes are clustered: one storm moves several cities, and all strikes
  of one event move together. Use block bootstrap by event and date. A winter
  is 15 to 30 independent bets, not hundreds.
- Report achievable size from real book depth next to any edge number.

## Phases and gates

0. Scaffold and environment. Done 2026-11-14.
1. Live logger. Kalshi book, market and trade logging on Railway since
   2026-11-14 16:12 UTC; `ercot-live` cron (D-046) since 2026-11-17.
2. Historical snow go/no-go: DONE 2026-11-15, CONDITIONAL (P2A). The market was
   more accurate than the NBM composite (D = -0.010, CI excludes 0). P2B DONE
   2026-11-17 (D-047): no documented NBM weakness. Do not re-run or retune P2A.
3. ERCOT go/no-go: DONE 2026-11-16, CONDITIONAL (P3A, D-038). The market was more
   accurate than ERCOT's own load forecast (D = -0.011, CI excludes 0).
   Settlement replication 1,334/1,334. Do not re-run or retune P3A.
4. Modelling: open by owner decision (D-040). Train / validate / held-out, choose
   on validation only, pre-register, score once. Frozen experiments and their
   evaluation dates are in `docs/preregistration.md` and `docs/commitments.md`;
   the work itself is private (D-053).
5. Paper trading, then tiny live with hard caps and a kill switch, only on
   written approval. Live trading is deployed from the private repo only.

Snow and ERCOT are separate strategies with shared plumbing. No cross-hedging.

## Hosting

Railway, Hobby plan, project `pm-weather`, US East: service `logger` (always
on), cron service `daily-report`, one-shot service `ercot-jobs` (the only one
holding ERCOT credentials), cron `paper-ercot` (ERCOT paper trading, deployed
from the private repo, D-055), bucket `pmw-data`. The $5 monthly credit is
shared with the owner's other projects (named in `CLAUDE.local.md`): never
touch those, and keep this project lean. Total account overage is acceptable up to
about $10 a month. Deploy with `railway up --service logger` from this folder;
there is no Railway config file in the repo (see D-023). A redeploy restarts
the logger and costs at most one 5-minute cycle of order books, so avoid
redeploying during an active storm unless it fixes a logging bug. Heavy jobs
run on the laptop. Details in `docs/deploy.md`.
