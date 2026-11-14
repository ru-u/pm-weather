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
make settle-check SERIES=KXNYCSNOWM
make pull-data      # copy the Railway bucket to ./data/bucket
```

Environment: Python 3.12 via `uv`; versions pinned in `uv.lock`. Core
dependencies are only what the logger needs. Heavy stacks are extras:
`research` (duckdb, pandas, scipy, ...), `modeling`, `weather` (herbie, xarray,
cfgrib), `energy` (gridstatus). `uv sync --all-extras` installs everything.

## Layout

```
configs/      series.yaml (discovery filters), stations.yaml, logger.yaml
docs/         decisions, preregistration, settlement_rules, data_sources, deploy, findings/
src/pmw/
  markets/    kalshi.py (public client), fees.py
  weather/    iem_cli.py (NWS CLI via IEM); nbm/hrrr/... to come
  energy/     ERCOT (Phase 3)
  logger/     discovery, schemas, buffer, runner, report
  settle/     snow.py (SNOWOVERTIME replication)
  features/ models/ eval/ backtest/   (later phases, empty)
  utils/      timeutil, storage, config
scripts/      pull_data.py, views.sql (DuckDB view layer)
tests/        offline; fixtures are real saved responses
data/         gitignored: raw/ interim/ processed/ live/
```

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

**Fees**
- Taker fee = 0.07 x multiplier x contracts x P x (1 - P), rounded up to
  $0.000001, then a rounding fee floors the balance change to $0.0001. All
  weather and energy series we track are `quadratic`, multiplier 1, no maker fee.
- The owner's Kalshi key is Ed25519 (PKCS#8 body stored without PEM header
  lines). Ed25519 signs the pre-sign text directly; RSA-PSS applies to RSA keys only.
- The fee-schedule PDF itself could not be fetched (kalshi.com rate-limits
  automated requests). The 0.07 was confirmed from the API docs' worked example.
  Reconcile against the first real fill.

**Settlement**
- Snow contracts (rulebook SNOWOVERTIME): source hierarchy is The Weather
  Company, then NWS, then OnTheSnow for ski areas. Last season's rules cited
  the NWS CLI report directly. "Above" is strictly greater. Trace is above 0.00
  but not above any positive strike. Missing data at the last expiration
  resolves every strike to No.
- NWS climate days run on local STANDARD time all year
  (`pmw.utils.timeutil.climate_day_bounds_utc`). Some storm-window markets say
  "local calendar days in Eastern Time" instead: read each market's rules.
- Stations come from the rules, never assumed. NYC is Central Park (KNYC).
- ERCOT daily peak: highest value in the TOTAL column of the first complete
  "Actual System Load by Forecast Zone" report (NP6-346-CD) for the operating
  day. Hourly averages, not 5-minute readings. The event ticker's date is the
  day AFTER the operating day (`KXTXERCOTPEAKD-26NOV15` is about Nov 14).

**Statistics**
- Snow outcomes are clustered: one storm moves several cities, and all strikes
  of one event move together. Use block bootstrap by event and date. A winter
  is 15 to 30 independent bets, not hundreds.
- Report achievable size from real book depth next to any edge number.

## Phases and gates

0. Scaffold and environment. Done 2026-11-14.
1. Live logger. Kalshi book, market and trade logging is deployed on Railway
   and running since 2026-11-14 16:12 UTC. NBM forecast snapshots, ERCOT
   forecast snapshots and Polymarket (read-only) logging still to do.
2. Historical snow go/no-go: do Kalshi snow prices beat, match or lose to free
   NBM guidance at matched timestamps? Plan in `docs/phase2_plan.md`, rule in
   `docs/preregistration.md` (frozen 2026-11-14; changes are logged deviations).
   History can only test monthly markets. Storm-window markets (42% of last
   winter's volume, six events) are tested prospectively on live-logged data.
3. ERCOT go/no-go (needs volume of about $20k/day on stressed days as well).
4. Modeling, only if a gate passes.
5. Paper trading, then tiny live with hard caps and a kill switch, only on
   written approval.

Snow and ERCOT are separate strategies with shared plumbing. No cross-hedging.
One combined risk layer caps total cold-weather exposure across both.

## Hosting

Railway, Hobby plan, project `pm-weather`, US East: service `logger` (always
on), cron service `daily-report`, bucket `pmw-data`. The $5 monthly credit is
shared with the owner's other projects (Zenith, uscom-logger): never touch
those, and keep this project lean. Total account overage is acceptable up to
about $10 a month. Deploy with `railway up --service logger` from this folder;
there is no Railway config file in the repo (see D-023). A redeploy restarts
the logger and costs at most one 5-minute cycle of order books, so avoid
redeploying during an active storm unless it fixes a logging bug. Heavy jobs
run on the laptop. Details in `docs/deploy.md`.
