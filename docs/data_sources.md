# Data sources

Every source, with URL, history, latency, quirks and terms. All free.
Last reviewed: 2026-11-14.

## Markets

### Kalshi Trade API (public REST)

- Base: `https://external-api.kalshi.com/trade-api/v2` (docs' recommended host;
  `https://api.elections.kalshi.com/trade-api/v2` also works).
- Docs: <https://docs.kalshi.com> (machine index at `/llms.txt`).
- Auth: none for market data (verified by live requests with no credentials:
  series, events, markets, orderbooks, trades, candlesticks, historical). The
  WebSocket needs a signed handshake, so we do not use it (D-003).
- Rate limits: unauthenticated limits are unpublished. About 8 requests/second
  from one IP returned 429. We use 2/second with exponential backoff.
- Endpoints we use:

| Endpoint | Notes |
| - | - |
| `GET /series?category=` | Category match is exact and case-sensitive. All series (no filter) is about 16,400 objects, 2.7 MB gzipped. |
| `GET /events?series_ticker=&status=open&with_nested_markets=true` | One call per series returns events and their markets with rule text. |
| `GET /markets?tickers=a,b,c` | Comma-separated, we send 100 per call. Gives bid/ask, volume, open interest, status, result. |
| `GET /markets/orderbooks?tickers=a&tickers=b` | Repeated parameter, **max 100 tickers**. Full depth. |
| `GET /markets/trades?ticker=&min_ts=` | All public prints with `created_time`. Paginated, 1000 per page. |
| `GET /series/{s}/markets/{t}/candlesticks` | `period_interval` 1, 60 or 1440 minutes. OHLC for trades, yes bid and yes ask. |
| `GET /historical/cutoff`, `/historical/markets`, `/historical/trades`, `/historical/markets/{t}/candlesticks` | Markets settled before the cutoff (2026-09-13 as of today) live only here. |

- Quirks:
  - Books are bids only, ascending, best last. YES ask = 1 - best NO bid.
  - Prices are 4-decimal dollar strings; sizes are fractional contracts.
  - An unknown ticker returns an empty book with HTTP 200, not 404.
  - The REST book has no exchange timestamp. We record our receive time.
  - `result` can be `yes`, `no` or `scalar` (settled at a price, as with the
    voided Chicago December 2025 markets).
  - ERCOT demand is in category Economics, power prices in Commodities.
- History available: KXNYCSNOWM and the other `KX*SNOWM` city series start
  December 2025. `KXSNOWNYM`/`SNOWNYM` (NYC monthly) go back to March 2024
  (17 events). `KXSNOWSTORM` has 6 settled events (Jan to Feb 2026).
- Terms: public API, used read-only at a low request rate.

### Kalshi fee schedule

- <https://kalshi.com/docs/kalshi-fee-schedule.pdf>. **Could not be fetched
  automatically** (HTTP 429 to non-browser clients). Fee logic was built from
  the API docs instead (`fee_type`, `fee_multiplier`, "Fee Rounding" page). See
  D-006 and the module docstring of `src/pmw/markets/fees.py`.
- `GET /series/fee_changes` lists scheduled fee changes per series. Not yet polled.

### Kalshi contract terms

- `https://assets.kalshi.com/contract_terms/<RULEBOOK>.pdf`, linked from each
  series object (`contract_terms_url`). Read so far: SNOWOVERTIME, ECONSTAT.

### Polymarket (read-only, not built yet)

- Gamma API (metadata), CLOB API (books), data API (trades). Polymarket.com
  resolves through the UMA oracle and rule wording can be ambiguous. Trading
  eligibility for the owner is unverified, so this is logging only.

## Settlement and ground truth

### NWS Daily Climate Report (CLI) via Iowa Environmental Mesonet

- `https://mesonet.agron.iastate.edu/json/cli.py?station=KNYC&year=2026`
- One row per station per climate day (local standard time). Fields used:
  `snow`, `snow_month`, `high`, `low`, `precip`, `product`.
- Values: number (inches), `"T"` trace, `"M"` or null missing.
- Quirks:
  - IEM keeps the LATEST CLI product per day. Kalshi settles on the initial
    non-preliminary value. Record `product` with every value used.
  - Some stations have days with no snowfall value at all (seen at KSEA, KBOS,
    KDFW, KAUS, KHOU, KORD in winter 2025-26). A fallback source is needed.
  - `snow_month` is NWS's own month-to-date total and is a useful cross-check
    on our daily sum (they agree for every NYC month tested).
- History: decades for first-order stations. Terms: IEM asks for reasonable use;
  we cache per station-year.

### The Weather Company dashboard

- <https://weather.com/kalshi>. First in Kalshi's source hierarchy for snow and
  temperature markets this season. No API known. To be checked by hand against
  CLI values at the first settlements.

### NOAA GHCN-Daily (not built yet)

- Longer station histories, for climatology and as a cross-check on CLI.

### ERCOT (not built yet)

- Public API `api.ercot.com/api/public-reports`, or `gridstatus`. Needs an ID
  token (username and password) plus the `Ocp-Apim-Subscription-Key` header.
  About 30 requests/minute. Reportedly blocks non-US IPs, so it runs from Railway.
- Settlement product: NP6-346-CD "Actual System Load by Forecast Zone", TOTAL
  column, first complete report per operating day.

## Forecasts

### NBM, National Blend of Models

- **Station text bulletins are archived on AWS**, per cycle, back to at least
  January 2021 (verified by listing):
  `https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.YYYYMMDD/HH/text/blend_nb{s,e,x,h,p}tx.tHHz`
  Each file is 20 to 35 MB of fixed-width text, one block per station.
- What matters for snow:
  - **NBP** (probabilistic): `S24P1/2/5/7/9` = 10th/25th/50th/75th/90th
    percentile of 24-hour snowfall, plus `Q24P*` (liquid) and `I24P*` (ice), for
    24-hour periods **ending 12Z**, out to about 9.5 days.
  - **NBS** (short range): `S06` 6-hour snowfall, `PSN/PZR/PPL/PRA`
    precipitation-type probabilities, `P06/P12`, `Q06/Q12`, 3-hourly temperature.
- The 12Z-to-12Z periods do NOT line up with a climate day (05Z to 05Z for
  Eastern stations). Handling that mismatch is part of the Phase 2 design.
- **Version is in every block header** (`NBM V4.3 NBP GUIDANCE 1/25/2026 0100
  UTC`). The bucket `noaa-nbm-pds` is organised by version: `blendv4.0`, `4.1`,
  `4.2`, `4.3`, `5.0`, `5.1`. So the archive spans at least six versions, and
  the bulletin on 2026-01-25 was still V4.3. Record the version per forecast.
- GRIB (percentile and exceedance grids) is in the same bucket under `core/`
  and `qmd/`. Use only if the text bulletins prove insufficient.
- Live: NOMADS. Terms: public domain (NOAA Open Data).

### HRRR, GEFS, ECMWF open data, MRMS (not built yet)

- Via Herbie, station points only, never full grids. Later phases.
