# Deployment on Railway

Host: Railway, Hobby plan, project **`pm-weather`**, region **US East**. The $5
monthly usage credit is shared with the owner's other Railway projects, which
nothing here touches. Total account overage is acceptable
up to about $10 a month (D-018).

Status: **deployed 2026-11-14, logger running since 16:12 UTC.**

## What is running

| Resource | Type | Start command | Settings | Secrets it holds |
| - | - | - | - | - |
| `logger` | always-on service | `pmw log` | region `us-east4`, restart policy ALWAYS, 1 replica | bucket credentials only |
| `daily-report` | cron service | `pmw report` | schedule `15 0 * * *` (00:15 UTC), region `us-east4`, restart policy NEVER | bucket credentials only |
| `pmw-data` | bucket | | region `iad` (US East) | |
| `ercot-live` | cron service, deployed 2026-11-17 04:37 UTC (D-046) | ERCOT dashboard load, latest NP3-565-CD posting, NBH for the Texas stations | hourly at :35 | bucket credentials; ERCOT credentials as references to `ercot-jobs` |
| `trader` | not built, separate later | | | the ONLY service that ever gets Kalshi keys |

Both services build from the repo's `Dockerfile` (auto-detected; core
dependencies only, no pandas, xarray, cfgrib or herbie). Neither has any
`KALSHI_*` or `ERCOT_*` variable, and nothing in their code reads one.

Project, environment, service and bucket IDs are not kept in the repo. They are
in the gitignored `CLAUDE.local.md`, or `railway status --json` prints them.

## Environment variables

Set on each service as Railway reference variables, so they follow the bucket.
The local `.env` is never deployed (`railway up` honours `.gitignore`, which
excludes `.env`, `keys.txt` and `data/`).

| Variable | Value |
| - | - |
| `PMW_S3_BUCKET` | `${{pmw-data.BUCKET}}` |
| `PMW_S3_ENDPOINT` | `${{pmw-data.ENDPOINT}}` |
| `PMW_S3_ACCESS_KEY_ID` | `${{pmw-data.ACCESS_KEY_ID}}` |
| `PMW_S3_SECRET_ACCESS_KEY` | `${{pmw-data.SECRET_ACCESS_KEY}}` |
| `PMW_S3_REGION` | `${{pmw-data.REGION}}` |
| `RAILWAY_DOCKERFILE_PATH` | `Dockerfile`. Pins the build to the Dockerfile (see the redeploy warning below). |
| `PMW_ALERT_WEBHOOK` | not set. Optional Discord or Slack webhook URL for gap and failure alerts. |

The bucket uses virtual-hosted URLs, which is the code's default.

## How it was set up (exact commands, for rebuilding from scratch)

```bash
railway init --name pm-weather --workspace "<workspace name>"
railway bucket create pmw-data --region iad
railway add --service logger \
  --variables 'PMW_S3_BUCKET=${{pmw-data.BUCKET}}' \
  --variables 'PMW_S3_ENDPOINT=${{pmw-data.ENDPOINT}}' \
  --variables 'PMW_S3_ACCESS_KEY_ID=${{pmw-data.ACCESS_KEY_ID}}' \
  --variables 'PMW_S3_SECRET_ACCESS_KEY=${{pmw-data.SECRET_ACCESS_KEY}}' \
  --variables 'PMW_S3_REGION=${{pmw-data.REGION}}'
railway scale --service logger us-east=1
railway variable set RAILWAY_DOCKERFILE_PATH=Dockerfile --service logger --skip-deploys
railway up --service logger --ci

# Settings that `railway environment edit` would not apply; set through the API:
M='mutation($s:String!,$e:String!,$i:ServiceInstanceUpdateInput!){serviceInstanceUpdate(serviceId:$s,environmentId:$e,input:$i)}'
railway api "$M" --raw-var s=<logger id> --raw-var e=<environment id> \
  --var 'i={"restartPolicyType":"ALWAYS","startCommand":"pmw log"}'

# daily-report: same `railway add` with the same five variables, then
railway api "$M" --raw-var s=<daily-report id> --raw-var e=<environment id> \
  --var 'i={"restartPolicyType":"NEVER","startCommand":"pmw report","cronSchedule":"15 0 * * *"}'
railway scale --service daily-report us-east=1
railway variable set RAILWAY_DOCKERFILE_PATH=Dockerfile --service daily-report --skip-deploys
railway up --service daily-report --ci
```

There is no Railway config file in the repo, on purpose (D-023): `railway.toml`
is deprecated and stops being read on 2026-12-01.

## Day-to-day

```bash
railway logs --service logger -n 50            # recent logs
railway metrics --service logger --memory      # memory over the last hour
railway usage projects                         # cost by project this billing period
railway run --service logger uv run pmw report --today --no-save --no-prune
                                               # report on today's data, read from the bucket
make pull-data                                 # copy the bucket to ./data/bucket (see Backups)
railway up --service logger --ci               # deploy the current folder to the logger
railway up --service daily-report --ci         # keep the report service on the same code
railway cron run --service daily-report --yes  # run the daily report now
railway cron list --service daily-report       # recent report runs and the next scheduled one
```

**Deploy with `railway up`, not `railway redeploy`.** On 2026-11-14 two
`railway redeploy --service logger` attempts were built with Railway's
auto-builder (Railpack) instead of the Dockerfile and failed with "No start
command detected". A failed deploy does not touch the running one, so no data
was lost, but the fix did not come from redeploy: `railway up` builds from the
Dockerfile every time. After any failed deployment, run `railway up` so the
latest deployment is a good one.

Healthy logs look like this: `logger starting: storage=s3:...`, then within
about 90 seconds `discovery: scanned ... tracking N markets`, then a
`base cycle N: books=... ok=True rss=...MB` line every 5 minutes and a `flush:`
line every 10 minutes.

A redeploy sends SIGTERM; the logger flushes its buffers and exits, and the new
instance re-pulls the last 24 hours of trades. Cost: at most one missed
5-minute cycle of order books. Avoid redeploying during an active storm unless
the deploy fixes a logging bug.

## What to watch in the first days

- **Memory.** Every `base cycle` and `flush:` line prints RSS, and the `health`
  table stores it per stage. Flat is healthy. A steady climb over a day means a
  leak: the first suspects are the trade-id dedupe set (capped at 200k ids) and
  the tracked-market map (closed markets are dropped after they settle).
- **Gaps.** The daily report lists any interval between base cycles longer than
  8 minutes. With `PMW_ALERT_WEBHOOK` set it also posts an alert.
- **429s.** A few `kalshi retry` warnings are normal, mostly during the hourly
  discovery. A constant stream means the pacing is too fast for Railway's
  shared egress IP: raise `min_request_interval_s` in `configs/logger.yaml`.
- **Cost.** `railway usage projects` after 48 hours, against the estimate below.

## Backups

- `make pull-data` copies the whole bucket to `./data/bucket` (incremental).
  Run it weekly, or more often during an active storm. It needs the five
  `PMW_S3_*` values in the local `.env`. Get them from the bucket's Credentials
  tab in the dashboard (bucket credentials only, not trading keys).
- A scheduled off-Railway copy (for example to Google Drive) is not built yet.
  Until it is, the weekly manual pull is the only backup.

## Measured on the laptop (2026-11-14) and what it implies for cost

Run: `pmw log --cycles 4` at production cadence, 18 minutes, 581 to 593 markets,
during a model-release burst window (so snow markets were on 1-minute books).

| Quantity | Measured |
| - | - |
| Base cycles / gaps / stage errors | 4 / 0 / 0 |
| Time to snapshot all books in a base cycle | 3 to 4 seconds (6 batched requests) |
| Requests | 755 total, of which about 330 were the one-time 24-hour trade backfill and 278 were two discovery runs |
| CPU | 3.8 CPU-seconds in 18 minutes (about 0.35% of one core) |
| Memory | about 95 MB until the first Parquet write, then 165 to 182 MB, flat across later flushes (the jump is pyarrow's Parquet and zstd libraries loading, not growth) |
| Rows | 4,934 order books, 2,372 market snapshots, 9,938 trades, 605 metadata versions |
| Parquet size | 29 bytes per order-book row, 22 per market row, 28 per trade |

Memory was measured on macOS. Linux in the container may differ by some tens of
MB either way, so check the first `flush:` lines after deploying. The target
was under 300 MB.

**Storage estimate.** About 300k order-book rows a day (288 base cycles of
roughly 590 markets, plus 1-minute books for hot markets and burst windows):

| | Per day | Per winter (mid Nov to end Mar, about 137 days) |
| - | - | - |
| Parsed Parquet (books, markets, trades, health, metadata) | 15 to 20 MB | 2 to 3 GB |
| Raw gzipped API responses | about 55 MB | rolling 14 days, about 0.8 GB |

Compression on a full day of more varied books will be somewhat worse than on
18 minutes, and the market count grows when January ladders list and when
storms bring new storm-window markets. Doubling everything still leaves the
bucket under 8 GB, which is about $0.12 a month at $0.015 per GB-month.

**Measured on Railway, first half hour (2026-11-14 16:15 to 16:45 UTC).** 647
markets. Base cycles at 16:15, 16:20, ... 16:45 with no gap across three
deployments (each outgoing container flushed on SIGTERM). In-process RSS 104 to
136 MB. Railway's own meter, which is what is billed: 64 MB current, 39 MB
average, 84 MB peak. First build took under two minutes. The daily-report job
runs in about 3 seconds. A handful of Kalshi 429s during discovery when two
containers overlapped, all retried successfully.

**Monthly cost estimate against the shared $5 credit** (rates from the brief:
about $10 per GB of RAM-month, $20 per vCPU-month), using Railway's metered
memory rather than the laptop figure:

| Item | Estimate |
| - | - |
| Logger memory, 0.07 to 0.09 GB metered | $0.70 to $0.90 |
| Logger CPU, under 1% of a vCPU | $0.10 to $0.20 |
| Egress (uploads to the bucket, about 2.5 GB a month) | about $0.15 |
| Bucket storage | $0.05 to $0.12 |
| `daily-report` cron (seconds a day) | under $0.05 |
| **Total** | **about $1.00 to $1.40 a month** |

Account usage from the owner's other projects was $3.96 for the billing period
ending 2026-11-16, so the account should land near
$5 to $6 a month in total, inside the $10 cap (D-018). Confirm with
`railway usage projects` after a few days: the memory figure is from half an
hour and may settle higher.

## `ercot-jobs` service (created 2026-11-16, owner approval D-035)

ERCOT calls must run from Railway US (CLAUDE.md rule 8), and ERCOT credentials
never sit on `logger` or `daily-report`. `ercot-jobs` runs one job per deploy
and stops (restart policy NEVER, no cron). Its service id
is in `CLAUDE.local.md`.

```bash
railway add --service ercot-jobs   # with the same five PMW_S3_* references as the logger
railway variable set RAILWAY_DOCKERFILE_PATH=Dockerfile --service ercot-jobs --skip-deploys
railway scale --service ercot-jobs us-east=1
# Credentials: piped from .env on stdin, so no value appears on a command line or in output.
#   railway variable set --service ercot-jobs --skip-deploys --stdin ERCOT_API_USERNAME  < (value)
#   ... ERCOT_API_PASSWORD, ERCOT_PUBLIC_API_SUBSCRIPTION_KEY (primary key only)
# Pick the job, then deploy:
railway api "$M" --raw-var s=<ercot-jobs service id> --raw-var e=<environment id> \
  --var 'i={"restartPolicyType":"NEVER","startCommand":"pmw ercot-probe"}'
railway up --service ercot-jobs --ci
railway logs --service ercot-jobs -n 50
```

Jobs run so far:
- 2026-11-16 02:06 UTC `pmw ercot-probe` (7 requests): both archives start in
  June 2017; NP6-346-CD 3,425 documents of about 1 KB, NP3-565-CD 82,218
  postings of about 66 KB.
- 2026-11-16 02:07 UTC `sh -c "pmw ercot-backfill np6-346-cd 2017-06-01
  2026-11-16 && pmw ercot-backfill np3-565-cd 2017-06-01 2026-11-16
  --post-hours 23,5,9,13,16"`. Forecasts are kept only for postings at
  23, 05, 09, 13 and 16 CT (the P3A decision slots), about 1.1 GB instead of 5.4 GB.

## `ercot-live` cron service (D-046)

One snapshot per run (`pmw ercot-live`, `src/pmw/energy/live.py`): ERCOT's public
dashboard (today's 5-minute demand), NBH bulletins for the five Texas stations
(cycles 1 and 2 hours back, block offsets kept in the bucket), every new
NP3-565-CD posting plus NP3-910-ER and NP6-346-CD of the last three days (through
`jobs.backfill`, same paths as the history), and Kalshi incentive programs once
per UTC day. Each part fails on its own; a run summary is written to
`live/ercot/runs/`. Schedule `5,35 * * * *` (twice an hour, just after each :30
forecast posting and after each hour's load), restart policy NEVER.

ERCOT credentials are Railway references to `ercot-jobs`, so the values are
stored once:

```bash
railway add --service ercot-live \
  --variables 'PMW_S3_BUCKET=${{pmw-data.BUCKET}}' \
  --variables 'PMW_S3_ENDPOINT=${{pmw-data.ENDPOINT}}' \
  --variables 'PMW_S3_ACCESS_KEY_ID=${{pmw-data.ACCESS_KEY_ID}}' \
  --variables 'PMW_S3_SECRET_ACCESS_KEY=${{pmw-data.SECRET_ACCESS_KEY}}' \
  --variables 'PMW_S3_REGION=${{pmw-data.REGION}}' \
  --variables 'ERCOT_API_USERNAME=${{ercot-jobs.ERCOT_API_USERNAME}}' \
  --variables 'ERCOT_API_PASSWORD=${{ercot-jobs.ERCOT_API_PASSWORD}}' \
  --variables 'ERCOT_PUBLIC_API_SUBSCRIPTION_KEY=${{ercot-jobs.ERCOT_PUBLIC_API_SUBSCRIPTION_KEY}}'
railway variable set RAILWAY_DOCKERFILE_PATH=Dockerfile --service ercot-live --skip-deploys
railway scale --service ercot-live us-east=1
railway api "$M" --raw-var s=<ercot-live id> --raw-var e=<environment id> \
  --var 'i={"restartPolicyType":"NEVER","startCommand":"pmw ercot-live","cronSchedule":"5,35 * * * *"}'
railway up --service ercot-live --ci
railway cron list --service ercot-live
```

**Cost estimate (before deploying).** A local run without ERCOT credentials took
about 40 s, most of it two first-time 28 MB NBH downloads; later runs use range
requests (about 10 small requests) plus the ERCOT calls, paced at one per 2.5 s
(token, three archive listings, one or two downloads), so about 20 to 30 s and
about 150 MB of memory. 48 runs a day is about 12 hours of runtime a month:
memory about 1.8 GB-hours ($0.03), CPU under $0.02, bucket writes about 2.5 MB a
day (75 MB a month, about $0.01 of storage and egress). **Total under $0.10 a
month**, against the roughly $1.0 to $1.4 for the rest of this project. Confirm
with `railway usage projects` after a week.

First run on Railway (manual trigger, 2026-11-17 04:38 UTC): 38 s, `ok=true`, 7
ERCOT requests; dashboard 272 points, both NBH cycles (two first-time full
downloads, offsets now in the bucket), 9,030 incentive programs, 43 forecast
postings of 2026-11-16/17 plus the newest NP3-910-ER and NP6-346-CD documents.

`railway run --service ercot-jobs ...` would run on the laptop with the
credentials injected; the client refuses (`RAILWAY_REPLICA_REGION` is absent).
Do not use it for ERCOT calls.

Pull the archive to the laptop with `make pull-data PREFIX=raw/ercot`, then
`uv run pmw ercot-build` and `uv run pmw ercot-settle-check`.
