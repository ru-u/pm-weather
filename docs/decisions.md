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

**D-026. ACIS is the second source for daily snowfall.** Used only for days
where the CLI report (via IEM) has no value, never to override a CLI value.
With it, every settled snow market outside the excluded Chicago December 2025
event reproduces (389 of 389). The settlement gate for Phase 2A is met for
last season's rules.

**D-027. Contract terms come from rule text first.** Period, station and strike
are parsed from each market's own rules when stated. A market whose rules name
a station that is not in `configs/stations.yaml` raises an error instead of
falling back to a default.

## 2026-11-15

**D-028. Logger hardened after the first night.** Every order-book stage
succeeded (851 of 851) but the market-snapshot call was rate-limited out 11
times: Railway's egress IP is shared and Kalshi limits by IP. Market snapshots
now keep the batches that worked, retries back off for up to about 50 seconds,
and pacing is 0.75 s per request. Redeployed 06:56 UTC.

**D-029. NBM comes from archived station text, by Range request.** Only the
01/07/13/19Z cycles carry the 24-hour snow percentiles, so only those are
pulled. Raw station blocks are kept verbatim per UTC day under
`data/raw/nbm/nbp/` with the S3 upload time of each file; the parsed table is
`data/interim/nbm/nbp_periods.parquet`. Availability time is the S3 upload
time when it is within 3 hours of the cycle, otherwise cycle + 2 hours.

**D-030. P2A pipeline built test-first, scoring last.** Order: composite model
with known-answer tests (single-period percentiles, Monte Carlo calibration,
copula correlation, alignment, no-lookahead) -> stage 1 forecasts with an
automated leakage check (0 violations in 41,723 rows) -> implementation
clarifications written into the pre-registration -> scoring code with the
registered synthetic runs (GO / NO GO / not GO as expected) -> real run.

**D-031. P2A result: CONDITIONAL; snow modeling is gated on P2B.** The market
was more accurate than the free NBM composite (D = -0.0103, CI excludes zero,
same sign in every registered variant); the naive taker strategy made +$508 on
33,653 contracts with an interval spanning zero and a perceived-vs-realized
edge correlation of 0.04. As registered: no Phase 4 snow model unless P2B finds
a specific NBM weakness. Priorities now: (1) ERCOT go/no-go (Phase 3),
(2) P2B, (3) keep logging for the prospective storm-window test P2A-S.

**D-032. A CLI value issued before its climate day ended is not final.** NWS
issues an intraday CLI ("valid today as of 5 PM") and IEM serves it until the
next-morning report replaces it; checking KXSNOWCITIES-26NOV15 on its last day
would otherwise have read Nov 15's 4 pm values as final. Such values, and any
day that has not ended, now count as missing in both sources. In history one
day is affected (KDCA 2026-02-21, final report never parsed by IEM); ACIS fills
it with the same 0.0, and replication is unchanged (389 reproduced, 0
contradicted). This season's rule text names stations as "City (KXXX)"; both
that and the old CLI product id are read. `settle-check --include-open` computes
markets that have not settled, without scoring them.

**D-033. First full logging day (2026-11-15) is clean after D-028.** 288 of 288
base cycles, no gaps, no alert. All 13 error rows that day (6 market-snapshot
429s, 4 trade fetches, 3 discovery) are before the 06:56 UTC redeploy; none
after. pm-weather has used $0.05 in its first 1.5 days of the billing period
(account total $4.14), in line with the ~$1 a month estimate.

**D-034. ERCOT client built and guarded; KXTXERCOTPEAK is not the target.**
`src/pmw/energy/ercot.py` (token + primary subscription key, 2.5 s pacing,
verbatim archive download) refuses to start outside a deployed Railway US
replica. Its tests use synthetic documents in the documented layout because no
real ERCOT response can be obtained before the `ercot-jobs` service exists; the
first probe run saves real ones to replace them. On the Kalshi side, every one
of the 1,334 settled KXTXERCOTPEAKD markets (78 operating days) parses, and
Kalshi's own expiration values reproduce every result under strictly-greater,
so replication reduces to matching our first-complete daily peak to Kalshi's
value. KXTXERCOTPEAK-27JAN01 (calendar-year 2026 peak, about 1.06M contracts)
settles on ERCOT's official settled peak, not the first report, has one
outcome, and is nearly decided (all strikes at 2 to 3 cents): it is context
for Phase 3, not a sample. Daily volume is concentrated in summer (median 18.9k
contracts per event, but 21 to 1,094 per day on Nov 10 to 14).

**D-035. Owner approved ERCOT credentials on a separate Railway service
(2026-11-16).** Option (a): Claude creates `ercot-jobs` and sets
`ERCOT_API_USERNAME`, `ERCOT_API_PASSWORD` and the primary
`ERCOT_PUBLIC_API_SUBSCRIPTION_KEY` from `.env` with `railway variable set`,
without printing values. The secondary key stays off Railway. `logger` and
`daily-report` never get ERCOT credentials.

**D-036. ERCOT settlement replication passes: 1,334 of 1,334.** NP6-346-CD was
backfilled on `ercot-jobs` (3,425 documents, 229 requests, 2017-06-29 to
2026-11-14), pulled with the logger's bucket credentials, and built locally.
Every settled KXTXERCOTPEAKD market reproduces; Kalshi's recorded values match
ours at their displayed precision (5 days are shown truncated or rounded). The
settlement gate (rule 6) is met for the P3A sample. NP3-565-CD forecasts are
backfilled for postings at 23, 05, 09, 13 and 16 CT only.

**D-037. Forecast backfill crashed once on bucket throttling; resumed.** At
02:45 UTC 2026-11-16 the NP3-565-CD backfill (at 2019-11) died when the Railway
bucket answered one upload with `SLOW_DOWN`; ERCOT itself was fine. ERCOT jobs
now retry bucket operations with backoff (2 s doubling to 60 s, 8 attempts).
Resumed at 04:01 UTC from 2019-11-01; documents already stored were skipped.

**D-038. P3A result: CONDITIONAL; ERCOT modeling is gated.** The Kalshi
KXTXERCOTPEAKD market was more accurate than probabilities from ERCOT's own
seven-day load forecast (D = -0.0110, CI excludes zero, same sign in every
registered variant). Not NO GO only because the 54-trade taker simulation made
+$261 (94% of resamples positive), with a negative perceived-versus-realized
edge correlation. Capacity in summer is adequate (median stressed day 37,855
contracts). As registered: no Phase 4 ERCOT model without a specific, documented
forecast weakness; next ERCOT step is a prospective test on live-logged books
through summer 2027 (P3A-S, to be registered). Both Phase 2 and Phase 3 now say
the same thing: the free public forecast is priced in.

**D-039. Kalshi account eligibility confirmed by the owner (2026-11-16).**
Kalshi support told the owner that no further emails are needed: eligibility is
decided by the site's geoblock and by completed identity verification, which the
owner has done. This removes a practical blocker for any future trading service.
It is not approval of live trading: rule 1 still requires the owner's written
approval here, and no strategy has passed a go/no-go gate (P2A and P3A are
CONDITIONAL). Research and logging use public endpoints and are unaffected.

**D-040. Owner starts Phase 4 modelling for ERCOT and snow (2026-11-16).**
After P2A and P3A came out CONDITIONAL, the owner decided to build models anyway,
following the nowcasting approach in a public write-up of a profitable Kalshi
hourly-temperature strategy (NGBoost distributional model on live observations
plus forecasts, log-odds blend with NBM, trading naive opening quotes). This
overrides the "documented forecast weakness" clause of the CONDITIONAL rule;
the rest of the discipline stands: models are trained on data before the test
period, chosen on a validation year, and scored once on a held-out period under
a pre-registered rule before any paper trading. ERCOT first (9 years of
actuals and forecasts already backfilled), then snow storm windows.

**D-041. ERCOT nowcast v1 beats the P3A baseline on validation by 20% (Brier).**
NP3-910-ER real-time load backfilled (3,455 days, 229 requests) and parsed
(early files are `48h_*` and 15-minute). LightGBM quantiles with conformal
calibration: validation Brier 0.072 against 0.090 for the P3A method, best at
every decision hour, gain growing through the day; intervals calibrated (81.5% /
95.5%). This licenses P4E (model vs Kalshi market), to be pre-registered before
any price is joined. See `docs/findings/2026-11-16-phase4-ercot-v1.md`.
