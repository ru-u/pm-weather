# Settlement rules

Exact rule text per series, our reading of it, and the edge cases. Rules can
change: the logger stores every market's rule text with a hash
(`market_meta.rules_sha256`), so a change shows up as a new metadata version.

Last reviewed: 2026-11-14.

---

## Snowfall: rulebook SNOWOVERTIME

Contract terms: <https://assets.kalshi.com/contract_terms/SNOWOVERTIME.pdf>
Covers `KX*SNOWM` (monthly city totals), `KXSNOWSTORM` and `KXSNOWCITIES`
(storm windows), `KXSNOWXMAS*`, `KXSNOWTHANKSGIVING`, `KXSNOWDAYS*`, and the
ski-resort series.

### Contract terms (quoted)

> **Underlying:** The Underlying for this Contract is the total snowfall in
> \<area\> \<time period\>. Revisions to the Underlying made after Expiration will
> not be accounted for in determining the Expiration Value.
>
> **Source Agency:** The Source Agencies are, in hierarchical order, The Weather
> Company (TWC), the National Weather Service, and OnTheSnow (for ski areas).
>
> **\<area\>:** ... This is the primary official weather measurement station for
> that location as designated by the Source Agency, or as specified by the Exchange.
>
> "Above" means greater than (>), "below" means less than (<), "exactly" means
> equal to (=) when rounded to two decimal places (unless otherwise specified),
> "at least" means greater than or equal to (>=), and "between" means within an
> inclusive range (>= lower bound and <= upper bound).
>
> **Payout Criterion:** ... rounded to the precision available from the Source
> Agency. A recording of "T" for Trace is considered above 0.00, and multiple
> recordings of "T" for Trace are considered above 0.00, but not necessarily
> above a specific positive number. If data is unavailable at the latest
> Expiration post, all strikes will resolve to No.
>
> Only initial non-preliminary reported values shall be used for the purpose of
> resolution unless otherwise explicitly specified by the Exchange. Corrections
> that the Exchange determines, in its sole discretion, do not remedy a material
> error shall not supersede earlier values, unless initial values released are
> preliminary.
>
> **Last Trading Date:** the final day of \<time period\>; Last Trading Time one
> minute prior to the end of \<time period\>.
> **Expiration Date:** latest one week after the final day of \<time period\>.
> **Expiration Time:** 10:00 AM ET.

### Market-level rule text, this season (KXNYCSNOWM-26DEC-2)

> If the total snowfall in New York City in December 2026 is strictly greater
> than 2 inches, then the market resolves to Yes.
>
> Snowfall data is available through The Weather Company's dashboard at
> https://weather.com/kalshi. Select "Snowfall," choose the city and station
> specified in this market's rules, and use "Custom" to select the specified
> calendar period. Snowfall is reported in inches for each station's local
> calendar day. ... Resolution uses initial non-preliminary official values
> reported by The Weather Company, subject to the contract's source hierarchy,
> correction, trace, and contingency provisions.

### Market-level rule text, last season (KXNYCSNOWM-26MAR)

> Data for CLINYC can be found by clicking the following URL:
> https://www.weather.gov/wrh/Climate?wfo=okx, navigating to the "Observed
> Weather" tab, and choosing the location "Central Park NY" with Daily Climate
> Report selected. Please use the latest version of the data for the desired date ...

### Storm-window markets, this season (KXSNOWCITIES-26NOV15-PVD)

> If the total snowfall recorded in Providence (KPVD) from Nov 12 to 15, 2026 is
> strictly greater than 1 inch, then the market resolves to Yes.
>
> ... Resolution counts all new snowfall recorded at the specified station from
> Nov 12 to 15, 2026 inclusive, using local calendar days in Eastern Time,
> regardless of which weather system caused it ... Exactly 1.0 inch does not
> satisfy the strictly-more-than-1-inch condition.

### Our interpretation (implemented in `src/pmw/settle/snow.py`)

| Question | Reading |
| - | - |
| What is summed | Daily snowfall at the settlement station over every day of the period. |
| Trace days | Add nothing to the sum. A period with only trace is "above 0" but not above 0.1. |
| Strike comparison | `strike_type=greater` is strictly greater. Exactly at the strike is No. |
| Missing day | A Yes already reached stands (more snow cannot undo it). Otherwise we return "undecided" rather than guess. Kalshi's own fallback is "all strikes No" if data is still unavailable at the last expiration. |
| Which report version | "Initial non-preliminary". IEM keeps the latest CLI product per day, so a late NWS correction can differ from what settled. We record the product id next to every value. A CLI product issued before its climate day ended (the 4-5 pm "valid today as of" report) is preliminary and counts as missing, as does any day that has not ended (`final_only`, added 2026-11-15). One historical day is affected (KDCA 2026-02-21, IEM kept only the 16:31 EST report); ACIS fills it with the same 0.0. |
| Station this season | Named by ICAO id in parentheses ("Providence (KPVD)"), not by CLI product. `find_station` reads either; an id not in `configs/stations.yaml` is an error. |
| Day boundary | NWS climate day, local standard time. Storm markets that say "local calendar days in Eastern Time" agree with this from early November to mid March (EST). A storm window in DST would be off by one hour at each end: flag any such market. |

### Open questions

1. **Does The Weather Company's value equal the NWS CLI value?** TWC is now
   first in the hierarchy. For first-order NWS stations it is most likely the
   same observation, but that is an assumption until this season's first
   settlements are checked. If they differ, TWC governs.
2. **Which station does each series use this season?** The new rule text says
   "the city and station specified in this market's rules" but the monthly rules
   name only the city. Confirm each on the TWC dashboard. New series with no
   settled history: KXDENSNOWM, KXMKESNOWM, KXMSPSNOWM, KXPITSNOWM, KXTTNSNOWM.
3. **Chicago.** Of 35 settled KXCHISNOWM markets, 29 cite CLIORD (O'Hare) and 6
   cite CLIMDW (Midway). The December 2025 event has five markets settled
   `scalar` (not yes/no) and a second event `KXCHISNOWM-25DECB` was listed.
   This looks like a station mix-up that Kalshi unwound. The re-listed event
   reproduces from O'Hare data (9.5 inches, equal to Kalshi's recorded value).
   The original event stays excluded.
4. **Missing CLI snow values: resolved 2026-11-14.** Days with no CLI snowfall
   value are filled from ACIS, the data service behind the NWS climate pages.
   ACIS had a value for every such day and agrees with the CLI wherever both
   exist. See `docs/findings/2026-11-14-settlement-replication.md`.
5. **Ski resorts** settle on OnTheSnow resort-reported totals, a different and
   less auditable source. Logged, not modelled.

### First settlement under this season's rules: KXSNOWCITIES-26NOV15

Five markets, one per station (KBOS, KEWR, KNYC, KPHL, KPVD), each "strictly
greater than 1 inch" over Nov 12-15, 2026, local calendar days in Eastern Time.
EST has been in effect since Nov 1, so these days equal the NWS climate days.
Trading closes 2026-11-16 04:59 UTC; expiration 2026-11-22 15:00 UTC.

Checked 2026-11-16 01:45 UTC (`pmw settle-check KXSNOWCITIES --include-open`):
**not settled yet.** Final CLI values for Nov 12-14:

| Station | Nov 12 | Nov 13 | Nov 14 | Total so far | Nov 15 (preliminary, 4-5 pm) |
| - | -: | -: | -: | -: | -: |
| KBOS | 0.0 | T | 0.0 | T | 0.0, no precip |
| KEWR | 0.0 | 0.0 | 0.0 | 0.0 | 0.0, no precip |
| KNYC | 0.0 | 0.0 | 0.0 | 0.0 | 0.0, no precip |
| KPHL | T | 0.0 | 0.0 | T | 0.0, no precip |
| KPVD | 0.0 | 0.0 | 0.0 | 0.0 | 0.0, no precip |

The storm brought rain (1.6 to 1.7 inches at KNYC and KEWR on Nov 12). Unless
Nov 15's final report shows more than an inch, which the dry preliminary makes
very unlikely, all five should resolve **No**. That would agree with the NWS but
would say little about TWC versus NWS values (open question 1), since every
total is zero or trace. **To do:** re-run after expiration and record Kalshi's
`result` and `expiration_value` here.

### Replication status

`uv run pmw settle-check all --brief`. As of 2026-11-14: 396 settled markets in
13 series (monthly city ladders, NYC back to November 2021, storm windows).
**389 reproduce, 0 contradict.** The other 7 are all in `KXCHISNOWM-25DEC`
(5 settled `scalar`, 2 cite Midway, which has no snowfall record), an event the
pre-registration excludes. Details in
`docs/findings/2026-11-14-settlement-replication.md`.

The contract's period, station and strike are read from each market's rule text
when the text states them (storm-window dates, the CLI product id, "strictly
greater than N"), and only otherwise from the ticker and `configs/stations.yaml`.
Markets Kalshi has not settled can be computed (not scored) with `--include-open`.

---

## ERCOT daily peak demand: KXTXERCOTPEAKD (rulebook ECONSTAT)

Contract terms: <https://assets.kalshi.com/contract_terms/ECONSTAT.pdf>
Settlement source named on the series: ERCOT data product **NP6-346-CD**,
"Actual System Load by Forecast Zone".

### Market-level rule text (KXTXERCOTPEAKD-26NOV15-T47000)

> If the highest hourly total system load reported for the ERCOT grid on
> November 14, 2026 is above 47,000 MW, then the market resolves to Yes.
>
> The Expiration Value is the highest value in the TOTAL column of the first
> complete "Actual System Load by Forecast Zone" CSV report published by ERCOT
> for the November 14, 2026 operating day.
>
> Each value represents ERCOT system load for the corresponding full hour.
> Instantaneous load readings displayed elsewhere by ERCOT will not be considered.
>
> Values will be compared as published without rounding. Later revisions,
> settlement adjustments, or republications will not affect the Expiration Value.

### Our interpretation

- Outcome variable: max over the 24 hourly rows of the TOTAL column for the
  operating day (Central prevailing time, hour-ending rows).
- "Above" is strictly greater, compared without rounding.
- First complete report governs. Later revisions are ignored, so the backfill
  must keep the FIRST published version of each day, not the latest.
- An hourly average is never above the highest 5-minute reading in that hour,
  so dashboard peaks overstate the settlement value.
- **Ticker date trap:** the event ticker carries the day AFTER the operating
  day. `KXTXERCOTPEAKD-26NOV15` is titled "peak electricity demand on Nov 14,
  2026" and closes 2026-11-15 05:55Z (11:55 PM CST on Nov 14).
- DST days have 23 or 25 hourly rows. Handle both.

Not yet replicated (Phase 3).

`KXTXERCOTPEAK` (one event, `-27JAN01`, about 1.06M contracts traded) is a
different question: the peak over a longer period. Rules not yet read.

---

## Daily low temperature: KXLOWT* (rulebook GLOBALTEMPERATURE)

Source named on the series: The Weather Company (weather.com/kalshi). Contract
terms not yet read in detail. Logged now; rules to be written up before any
analysis.

## Power prices: KXERCOT*, KXPJM*, KXMISO* (weekly and monthly hub averages)

"Average real-time power price" per hub. Rules not yet read. Later phase.
