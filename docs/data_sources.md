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

### ACIS (Applied Climate Information System)

- `POST https://data.rcc-acis.org/StnData` with `{"sid": "KSEA", "sdate": ...,
  "edate": ..., "elems": [{"name": "snow"}]}`. No key. Accepts ICAO ids.
- The data service behind the NWS "NOWData" climate pages: the same official
  station record, per day. Values: number string, `"T"` trace, `"M"` missing; a
  trailing letter (for example `"1.2A"`) flags a multi-day accumulation, which
  we treat as missing.
- Role here: second source for daily snowfall, used only on days where the CLI
  report has no value. Agrees with CLI on every day checked where both exist.
- Like IEM, it serves the latest value, not the initial one.
- Also the natural source for long station histories and climatology (Phase 2,
  step 3), instead of GHCN-Daily.

### The Weather Company dashboard

- <https://weather.com/kalshi>. First in Kalshi's source hierarchy for snow and
  temperature markets this season. No API known. To be checked by hand against
  CLI values at the first settlements.

### Station climatology

- `pmw climo-pull` writes `data/interim/climo/daily_snow.parquet`: daily
  snowfall from ACIS for every configured station, 1991-07 onward.
- Winter days with no value: none to 0.2% at most stations, but Seattle 21%,
  Austin 17%, Houston 35%, and Midway 97% (Midway has essentially no snowfall
  record, which is why Kalshi's Midway-cited Chicago markets were unwound).
  Climatology must be built from non-missing days only.
- GHCN-Daily is no longer needed for this purpose.

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
- **Only the 01Z, 07Z, 13Z and 19Z cycles carry the 24-hour percentiles.** An
  NBP file exists for every hour, but at other hours a station block holds only
  wind and pressure rows (or nothing). Verified on 2026-01-24.
- **Layout is fixed-width, 80 columns, and a station sits at the same byte
  offset in every cycle** until the station list or version changes (3,807
  bytes per CONUS station in V4.3, 2,997 in V4.0). `pmw nbm-pull` therefore
  fetches each station with one 4 KB Range request and downloads a whole file
  only to relearn offsets: ten days of 21 stations take about 18 seconds.
- **Upload lag.** The S3 `Last-Modified` of these four cycles is normally 68 to
  101 minutes after the cycle (01Z at about 02:09 to 02:41, 13Z at about 14:08
  to 14:36). Some days were uploaded many hours late in one batch
  (2026-01-25), so `Last-Modified` is trusted as availability only within 3
  hours of the cycle; otherwise cycle + 2 hours is used
  (`pmw.weather.nbm_text.available_at`).
- **Measured over five winters (3,023 cycles, 2021-11 to 2026-03):** median
  upload lag 69 minutes for the 07Z and 19Z cycles and 87 minutes for 01Z and
  13Z; 90th percentile about 99 minutes; under 1% uploaded more than 3 hours
  late. Coverage is 3,022 of 3,040 expected cycles. Percentiles are monotone in
  every parsed period.
- **Versions seen in the sample:** V4.0 until 2023-01-17, V4.1 from 2023-01-17
  through winter 2023-24, V4.2 in winter 2024-25, V4.3 in winter 2025-26.
- Consequence for the registered decision times (02:30, 08:30, 14:30, 20:30
  UTC): a cycle that lands a few minutes late is not yet available, and the
  previous major cycle, about six hours older, is the one a trader could
  actually have used. The join rule handles this by construction.
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
