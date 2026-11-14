# Decisions

Every non-obvious choice, dated and numbered. Newest at the bottom. A decision
that reverses an earlier one says so and leaves the earlier entry in place.

Live trading approval, if it is ever given, is recorded here by the owner.
**As of 2026-11-14 live trading is NOT approved.**

---

## 2026-11-14

**D-001. Own git repository for this project.** The project folder sat untracked
inside the owner's existing `Poly` repo (the PrediCup bot). This project gets
its own repo (`git init` here) because it deploys separately, has its own
secrets, and is meant to be shown on its own. The parent repo now sees this
folder as an untracked nested repo.

**D-002. Secrets standardised to `.env`.** The existing key file was `keys.txt`
(gitignored, never committed: `git log --all -- keys.txt` is empty). A `.env`
was generated from it with the variable names `gridstatus` and this repo
expect. `keys.txt` was left in place for the owner to delete. Both are
gitignored, along with `*.pem`, `*.key` and `.env.*`. Only the ERCOT primary
subscription key is used; the secondary is kept under its own name for rotation.

**D-003. The logger uses REST polling, not the WebSocket.** Kalshi's market-data
REST endpoints are public, but the WebSocket requires API-key headers in the
handshake (docs: "Authentication is required to establish the connection").
The brief's rule is that a logger which needs no key must not load one, so the
logger polls REST. Cost: no tick-by-tick book changes, only snapshots (5 min,
1 min when hot). Trades are unaffected because the trades endpoint returns
every print with its exchange timestamp. Revisit only if snapshot granularity
proves too coarse for the backtester, and then with a key that has read-only
scope if Kalshi offers one.

**D-004. Snow settlement source changed this season; treat station mappings as
provisional.** The 2026-27 snow and temperature markets name The Weather
Company's dashboard (weather.com/kalshi) as the source, ahead of the NWS in the
contract's hierarchy. Last season's markets cited the NWS CLI product by name.
We keep computing outcomes from NWS CLI data (it reproduced every decidable
settled market, see `docs/findings/2026-11-14-recon.md`) and will check the
first settlements of this season against it. The brief's statement that snow
"settles on the NWS Daily Climate Report" is no longer the whole story.

**D-005. Base URL.** Kalshi's docs recommend
`https://external-api.kalshi.com/trade-api/v2` for API traders. The brief
suggested `api.elections.kalshi.com`, which is still supported. We use the
recommended host.

**D-006. Fee function built from the API docs, not the fee-schedule PDF.**
kalshi.com returned HTTP 429 to every automated request for the PDF. The API
docs give the fee types, the maker multipliers and the rounding mechanics, and
their worked example pins the taker coefficient at 0.07. Open item: the owner
reads the PDF in a browser and we reconcile one real fill. Until then the
maker-fee fractions are "per API docs".

**D-007. Parquet layout is `source/table/date`, with series as a column.** The
brief asked for partitions by `source/date/series`. With about 136 matched
series and a flush every 10 minutes, per-series files would mean tens of
thousands of tiny objects a day. One file per table per flush, partitioned by
UTC date, keeps object counts near 150 a day per table. Series filters are
cheap because the column is low-cardinality and dictionary-encoded.

**D-008. Orderbook rows are one per market per snapshot, with list columns.**
Each row holds the full depth as four lists (yes/no price and size, exactly as
received) plus derived top of book. This is lossless, about 10x smaller than
one row per level, and DuckDB unnests it when needed.

**D-009. Trades are fetched only for markets whose volume moved, in idle time.**
There is no "all trades for a series" endpoint, and the unfiltered tape covers
all of Kalshi. Each base cycle the market snapshot shows which markets traded;
those are queued and fetched one request at a time between snapshots, so trade
polling never delays a book. On restart the logger re-pulls 24 hours per
market. Duplicates are expected and removed by `trade_id` at read time.

**D-010. Low-temperature markets cover all US stations, at base cadence.** The
brief asked for "cold cities". All US stations cost only three more batched
requests per cycle and avoid choosing cities before looking at data.
International stations are excluded. Low-temp markets go hot only on a volume
spike, not merely because they close soon (every daily market closes soon).

**D-011. Discovery scans three categories, not all 16k series.** Climate and
Weather, Economics (ERCOT demand) and Commodities (power prices) cover every
target. `pmw discover --all-categories` scans everything and should be run by
hand now and then to catch a series filed somewhere unexpected. Checked on
2026-11-14: the full scan matched 10 more series and every one was a false
positive (Snowflake KPIs, Lowe's, Lower Saxony, Snow White, snowboarding). So
the category restriction is also what keeps the regexes honest: the logger must
not be switched to the full scan without tightening the filters first.

**D-012. Model-release burst windows are approximate.** `configs/logger.yaml`
lists UTC windows when snow markets go to 1-minute books (GFS/GEFS and ECMWF
arrival). The windows are estimates of public availability and must be checked
against real file timestamps on NOMADS and ECMWF open data before any analysis
leans on them.

**D-013. Folder name stays `snowcot`; the package is `pmw`.** The brief's tree
calls the repo `pm-weather`. Renaming the folder would break the owner's
existing workspace for no gain.

**D-014. `scripts/` holds operational helpers; the CLI lives in the package.**
The typer app is `pmw.cli` (installed as the `pmw` command) so the Docker image
and the laptop run the same entry point. `scripts/` has `pull_data.py` and the
DuckDB view layer.

**D-015. Unauthenticated pacing is 2 requests per second.** Two processes at
4 requests per second each from one IP drew 429s; one process at that rate did
not. A base cycle needs about 15 requests, so 2/s is ample.

**D-016. Not built yet, on purpose.** Polymarket logging (read-only, lower
priority), NBM forecast snapshots, and ERCOT forecast snapshots. NBM text
bulletins and GRIB are archived publicly, so nothing is lost by logging them a
few days late; Kalshi orderbooks are the only data here that cannot be
recovered later, which is why they came first.

**D-017. Initial commit made on `main`, local only.** Nothing has been pushed
and no remote is configured. Before the first push, decide whether the GitHub
repo is private (recommended until Phase 2 is written up).

**D-018. Budget: overage on the shared Railway account is acceptable up to about
$10 a month in total** (owner, 2026-11-14). This project's estimate is $2.20 to
$2.50 a month, so the cap is only at risk if the other services already use
more than about $7.50.

**D-019. Correction: the Kalshi private key in the key file is valid.** The
first-session note called it incomplete because it is one 64-character line. It
decodes to a 48-byte PKCS#8 Ed25519 private key, which is the key type Kalshi
now generates by default (API docs, "API Keys"). It is stored without the
`-----BEGIN PRIVATE KEY-----` / `-----END PRIVATE KEY-----` lines; the future
trading service adds them when loading. Signing with an Ed25519 key signs the
pre-sign text directly (no RSA-PSS). Nothing in the logger reads this key.

**D-020. `.env` re-synced after the owner corrected the ERCOT username in
`keys.txt`.** `keys.txt` and `.env` now hold the same values. Keep one of them:
edits made to only one file will drift again.

**D-021. Pre-registration P2A frozen.** The three open choices (spread filter
0.20, pooled primary metric, 90% P&L threshold) were kept as drafted after a
design analysis that used quotes and volumes only
(`docs/findings/2026-11-14-prereg-design.md`). Added: sensitivity checks at
spreads 0.10 and 0.30, a monthly-only scope for any GO, and a prospective
storm-window experiment on live-logged data.

**D-022. Deployed to Railway on 2026-11-14 (owner said "deploy").** New project
`pm-weather` in the owner's workspace, separate from Zenith and uscom-logger,
which were not touched. Bucket `pmw-data` in US East (`iad`), always-on service
`logger` and cron service `daily-report` (00:15 UTC), both in US East
(`us-east4`). Neither service has any `KALSHI_*` or `ERCOT_*` variable. Account
usage before this project was $3.96 for the billing period (Zenith $2.41,
uscom-logger $1.55), so the expected total stays under the $10 cap in D-018.

**D-023. No Railway config file in the repo.** Railway deprecated
`railway.toml` (it stops being read on 2026-12-01) in favour of
`.railway/railway.ts`, which needs a Node package installed to plan or apply.
For two small services that is not worth a second toolchain. `railway.toml` was
removed. The Dockerfile is auto-detected, and the remaining settings (start
command, restart policy, cron schedule, region) are stored on the Railway
services and listed with the exact commands in `docs/deploy.md`. Note for
later: `railway environment edit --service-config` reported "No changes to
apply" for these settings, so they were set through the GraphQL API
(`serviceInstanceUpdate`) instead.

**D-024. Builds are pinned to the Dockerfile with `RAILWAY_DOCKERFILE_PATH`, and
deploys use `railway up`.** `railway redeploy` twice rebuilt the logger with
Railpack and failed (harmlessly: the running deployment was untouched). The
latest logger deployment is a successful `railway up` build. See
`docs/deploy.md`.

**D-025. The first daily report will raise one expected alert.** Logging began
at 16:15 UTC on 2026-11-14, so the report for that date has about 93 of 288 base
cycles and trips the "under 95% of a full day" check. Reports from 2026-11-15
onward should be clean; a gap or shortfall after that date is real.
