# 2026-11-14: settlement replication for every settled snow market

Phase 2, step 2. Reproduce with `uv run pmw settle-check all --brief`.

## Result

| Series | Markets | Reproduced | Contradicted | Undecided | Not yes/no | Used ACIS for a day |
| - | -: | -: | -: | -: | -: | -: |
| KXNYCSNOWM | 28 | 28 | 0 | 0 | 0 | 0 |
| KXBOSSNOWM | 24 | 24 | 0 | 0 | 0 | 18 |
| KXDCSNOWM | 24 | 24 | 0 | 0 | 0 | 0 |
| KXPHILSNOWM | 24 | 24 | 0 | 0 | 0 | 0 |
| KXCHISNOWM | 35 | 28 | 0 | 2 | 5 | 8 |
| KXDETSNOWM | 24 | 24 | 0 | 0 | 0 | 0 |
| KXSEASNOWM | 20 | 20 | 0 | 0 | 0 | 15 |
| KXSLCSNOWM | 11 | 11 | 0 | 0 | 0 | 0 |
| KXDALSNOWM | 20 | 20 | 0 | 0 | 0 | 5 |
| KXAUSSNOWM | 16 | 16 | 0 | 0 | 0 | 4 |
| KXHOUSNOWM | 16 | 16 | 0 | 0 | 0 | 4 |
| KXSNOWNYM / SNOWNYM (NYC, 2021 to 2025) | 91 | 91 | 0 | 0 | 0 | 11 |
| KXSNOWSTORM (storm windows) | 63 | 63 | 0 | 0 | 0 | 0 |
| **Total** | **396** | **389** | **0** | **2** | **5** | **65** |

**Every market outside the Chicago December 2025 event reproduces: 389 of 389.**
The seven that do not are all in `KXCHISNOWM-25DEC`, which the pre-registration
already excludes: five were settled by Kalshi at a price (`scalar`) and two
cite Chicago Midway, which has no snowfall record in either source.

## What changed since the morning's check (203 of 242)

1. **Second source for missing days.** The CLI report (via IEM) has no snowfall
   value on some days at Seattle, Boston, Dallas, Austin, Houston, O'Hare and
   Central Park. ACIS, the data service behind the NWS climate pages, has a
   value for every one of them (all 0.0 or trace). It is used ONLY for days the
   CLI lacks, and each result records how many days it filled.
   - Cross-check: on the nine station-months first examined, ACIS equals the
     CLI value on all 252 days where both exist.
   - Cross-check: where Kalshi recorded its own settlement value, our totals
     built with ACIS-filled days equal it (Boston 26.8, 29.8 and 1.9; Seattle
     3.0; Dallas 2.4).
2. **Contract terms read from each market's rule text.** Period, station and
   strike come from the text when it states them. This mattered three times:
   - Storm windows have arbitrary dates ("Jan 30, 2026 through Feb 3rd, 2026").
   - The first NYC market covered November 8 to 30, 2021, not the month.
   - Markets before late 2023 carry no strike fields; the strike is in the text.
3. **Older NYC series and storm windows added**: 154 more markets, all reproduce.

## Kalshi's recorded value versus ours

Kalshi stores an `expiration_value` on some markets. Ours equals it everywhere
except two NYC events, and neither affects an outcome:

| Event | Ours | Kalshi recorded | Reading |
| - | - | - | - |
| SNOWNYM-23MAR | 0.1 | 0 | Their field is rounded to whole inches in that era (February 2022 is "2" for 2.0). |
| SNOWNYM-23NOV | T | 0.5 | 0.5 is the strike of that event's market; looks like a data-entry artifact. The market settled No, consistent with trace. |

## Caveats that remain

- Both sources give the LATEST value for a day; Kalshi's rule is the initial
  non-preliminary value. No case found where that changed an outcome, but the
  check cannot rule out a market that settled on a value later corrected.
- This validates last season's rules, which cited the NWS report. This season
  The Weather Company is first in the hierarchy. The first test of that is the
  settlement of `KXSNOWCITIES-26NOV15` (window ends 2026-11-15).
