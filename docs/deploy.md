# Deploying on Railway

Host: Railway, Hobby plan. One **new, separate project** for this repo, in
**US East**. The $5 monthly usage credit is shared with the owner's other
projects (Zenith and an existing Polymarket logger). Nothing here touches them.

Status 2026-11-14: **not deployed yet.** The logger has been run locally only.
The Docker image has not been built (Docker was not running on the laptop), so
the first deploy is also the first build: watch the build log.

## Service layout

| Service | Type | Start command | Holds which secrets |
| - | - | - | - |
| `logger` | always on | `pmw log` | bucket credentials only |
| `daily-report` | cron, `15 0 * * *` (00:15 UTC) | `pmw report` | bucket credentials, optional alert webhook |
| `forecast-snapshot` | cron (later) | to be built: NBM text bulletins, ERCOT load forecast | bucket credentials, ERCOT credentials |
| `trader` | later, separate | not built | the ONLY service that ever gets Kalshi keys |
| `pmw-data` | bucket | | |

All services build from the same `Dockerfile` (core dependencies only: no
pandas, xarray, cfgrib or herbie). The logger and the cron jobs never receive
`KALSHI_*` variables. There is nothing in their code that reads them.

Railway cron schedules are in UTC and the service must exit when its work is
done, which `pmw report` does.

## Environment variables

Set in the Railway dashboard per service. The local `.env` is never deployed.

| Variable | logger | daily-report | Value |
| - | - | - | - |
| `PMW_S3_BUCKET` | yes | yes | `${{pmw-data.BUCKET}}` |
| `PMW_S3_ENDPOINT` | yes | yes | `${{pmw-data.ENDPOINT}}` |
| `PMW_S3_ACCESS_KEY_ID` | yes | yes | `${{pmw-data.ACCESS_KEY_ID}}` |
| `PMW_S3_SECRET_ACCESS_KEY` | yes | yes | `${{pmw-data.SECRET_ACCESS_KEY}}` |
| `PMW_S3_REGION` | yes | yes | `${{pmw-data.REGION}}` |
| `PMW_ALERT_WEBHOOK` | optional | optional | Discord or Slack webhook URL |
| `KALSHI_*` | **never** | **never** | |
| `ERCOT_*` | no | no | only on `forecast-snapshot`, when it exists |

`BUCKET`, `ENDPOINT`, `ACCESS_KEY_ID`, `SECRET_ACCESS_KEY` and `REGION` are the
variable names a Railway bucket exposes (Railway docs, Storage Buckets). The
`${{pmw-data.NAME}}` form is a Railway reference variable; `pmw-data` is
whatever you name the bucket. Railway buckets use virtual-hosted URLs, which is
the code's default. If the bucket's Credentials tab says path-style, also set
`PMW_S3_PATH_STYLE=1`.

## Walkthrough

You do steps 1 to 6 in the dashboard or CLI; they create billable resources, so
they were not done for you.

1. **Create the project.** Dashboard, New Project, Empty Project. Name it
   `pm-weather`. Confirm you are NOT inside the Zenith project.
   CLI alternative, from this folder: `railway init` and pick "Empty Project".
2. **Add the bucket.** In the project: Create, Bucket. Name it `pmw-data`.
3. **Push the code somewhere Railway can build it.** Either connect a private
   GitHub repo (New, GitHub Repo), or deploy straight from the laptop with
   `railway up` from this folder (uses `.dockerignore`, so `.env`, `keys.txt`
   and `data/` are not uploaded).
4. **Configure the `logger` service.**
   - Settings, Region: **US East (Virginia)**.
   - Settings, Build: Dockerfile (already set by `railway.toml`).
   - Settings, Deploy: start command `pmw log`, restart policy Always, 1 replica.
   - Variables: the five `PMW_S3_*` rows from the table above.
   - Do not add any `KALSHI_*` or `ERCOT_*` variable.
5. **Deploy and read the first minutes of logs.** Expect, in order:
   `logger starting: storage=s3:...`, then within about 90 seconds
   `discovery: scanned ... tracking N markets`, then a `flush:` line every 10
   minutes that ends with `rss=...MB`.
6. **Add the `daily-report` cron service.** New service from the same repo,
   same variables, start command `pmw report`, Cron Schedule `15 0 * * *`,
   region US East.
7. **Check after one hour:** `railway run uv run pmw report --today --no-save --no-prune` from the laptop
   (or trigger the cron service once) and confirm base cycles are arriving every
   5 minutes with no gaps.

## What to watch in the first days

- **Memory.** Every `flush:` log line prints RSS, and the `health` table stores
  it per stage. Flat is healthy. A steady climb over a day means a leak: the
  first suspects are the trade-id dedupe set (capped at 200k ids) and the
  tracked-market map (closed markets are dropped after they settle).
- **Gaps.** The daily report lists any interval between base cycles longer than
  8 minutes and posts to `PMW_ALERT_WEBHOOK` if a day has gaps.
- **429s.** A few `kalshi retry` warnings are normal. A stream of them means the
  pacing is too fast for Railway's shared egress IP: raise
  `min_request_interval_s` in `configs/logger.yaml`.
- **Usage page.** Compare the project's daily cost with the estimate below
  after 48 hours.

## Backups

- `make pull-data` copies the whole bucket to `./data/bucket` (incremental).
  Run it weekly, or more often during an active storm. It needs the five
  `PMW_S3_*` values in the local `.env` (bucket credentials only).
- A scheduled off-Railway copy (for example to Google Drive) is not built yet.
  Until it is, the weekly manual pull is the backup.

## Updating

Config changes (`configs/*.yaml`) ship with the image, so they need a redeploy.
On redeploy Railway sends SIGTERM; the logger flushes its buffers and exits
cleanly, then the new instance re-pulls the last 24 hours of trades, so a
redeploy costs at most one missed base cycle of order books.

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

**Monthly cost estimate against the shared $5 credit** (rates from the brief:
about $10 per GB of RAM-month, $20 per vCPU-month):

| Item | Estimate |
| - | - |
| Logger memory, about 0.18 GB | $1.80 |
| Logger CPU, under 1% of a vCPU | $0.10 to $0.20 |
| Egress (uploads to the bucket, about 2.5 GB a month) | about $0.15 |
| Bucket storage | $0.05 to $0.12 |
| `daily-report` cron (seconds a day) | under $0.05 |
| **Total** | **about $2.20 to $2.50 a month** |

That leaves roughly half the credit for Zenith and the existing Polymarket
logger. If their combined usage is already above about $2.50, this project
pushes the account past the included $5 and the overage is billed. Check the
account's current usage before deploying.
