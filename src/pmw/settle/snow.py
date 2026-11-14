"""Settlement replication for Kalshi snowfall contracts (rulebook SNOWOVERTIME).

Contract terms (https://assets.kalshi.com/contract_terms/SNOWOVERTIME.pdf, read
2026-11-14), the parts that decide an outcome:

  * Underlying: total snowfall at the settlement station over the time period,
    i.e. the sum of the daily values over the period's local calendar days.
  * Comparison operators: "above" is strictly greater (>), "below" is strictly
    less (<), "at least" is >=, "between" is inclusive on both ends.
  * Trace: 'A recording of "T" for Trace is considered above 0.00, and multiple
    recordings of "T" are considered above 0.00, but not necessarily above a
    specific positive number.' So trace days add nothing to the sum, but a
    period whose only snow was trace still counts as "above 0".
  * Missing data: "If data is unavailable at the latest Expiration post, all
    strikes will resolve to No."
  * Source hierarchy (current terms): The Weather Company, then the National
    Weather Service, then OnTheSnow for ski areas. Markets settled before the
    2026-27 season cited the NWS Daily Climate Report directly.

This module computes outcomes from NWS CLI data. That reproduced every settled
market tested so far, but it is the SECOND source in the current hierarchy, so
every new season's settlements must be re-checked against it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from pmw.markets.kalshi import KalshiPublic
from pmw.utils.config import Station
from pmw.utils.timeutil import month_days
from pmw.weather.iem_cli import CliDay, IemCli

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1
    )
}


@dataclass(frozen=True)
class PeriodTotal:
    """Snowfall over a period, keeping trace and missing-day information."""

    inches: Decimal  # sum of measurable daily values
    any_trace: bool
    n_days: int
    n_missing: int  # days with no report or a missing value

    @property
    def complete(self) -> bool:
        return self.n_missing == 0


def period_total(days: dict[date, CliDay], period: list[date]) -> PeriodTotal:
    total = Decimal(0)
    any_trace = False
    missing = 0
    for d in period:
        row = days.get(d)
        if row is None or row.snow.missing:
            missing += 1
            continue
        any_trace = any_trace or row.snow.trace
        total += row.snow.measurable
    return PeriodTotal(total, any_trace, len(period), missing)


def resolve(
    total: PeriodTotal,
    strike_type: str,
    floor_strike: Decimal | None,
    cap_strike: Decimal | None,
) -> bool | None:
    """Outcome (True = Yes) of one strike, or None if the data cannot decide it.

    `strike_type` is Kalshi's market.strike_type: greater, greater_or_equal,
    less, less_or_equal, between.
    """
    x = total.inches

    def above(k: Decimal) -> bool:
        # Trace counts as above 0.00 but not above any positive number.
        return x > k or (k == 0 and x == 0 and total.any_trace)

    if strike_type == "greater":
        assert floor_strike is not None
        yes = above(floor_strike)
        # Missing days can only add snow, so a Yes on an "above" strike is safe.
        return True if yes else (False if total.complete else None)
    if strike_type == "greater_or_equal":
        assert floor_strike is not None
        yes = x >= floor_strike
        return True if yes else (False if total.complete else None)
    if not total.complete:
        return None
    if strike_type == "less":
        assert cap_strike is not None
        return x < cap_strike
    if strike_type == "less_or_equal":
        assert cap_strike is not None
        return x <= cap_strike
    if strike_type == "between":
        assert floor_strike is not None and cap_strike is not None
        return floor_strike <= x <= cap_strike
    raise ValueError(f"unsupported strike_type {strike_type!r}")


def parse_monthly_event(event_ticker: str) -> tuple[int, int]:
    """'KXNYCSNOWM-26DEC' -> (2026, 12).

    A single trailing letter is allowed: Kalshi listed a second Chicago event for
    one month as 'KXCHISNOWM-25DECB'.
    """
    m = re.search(r"-(\d{2})([A-Z]{3})[A-Z]?$", event_ticker)
    if not m or m.group(2) not in _MONTHS:
        raise ValueError(f"not a monthly event ticker: {event_ticker}")
    return 2000 + int(m.group(1)), _MONTHS[m.group(2)]


@dataclass(frozen=True)
class SettlementCheck:
    ticker: str
    event_ticker: str
    strike_type: str
    floor_strike: Decimal | None
    cap_strike: Decimal | None
    kalshi_result: str  # "yes" / "no" / other
    total: PeriodTotal
    computed: bool | None

    @property
    def match(self) -> bool:
        if self.kalshi_result not in ("yes", "no") or self.computed is None:
            return False
        return (self.kalshi_result == "yes") == self.computed


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def check_monthly_series(
    client: KalshiPublic, iem: IemCli, series_ticker: str, station: Station
) -> list[SettlementCheck]:
    """Recompute every settled market of a monthly snow series from CLI data."""
    checks = []
    for m in client.settled_markets(series_ticker):
        year, month = parse_monthly_event(m["event_ticker"])
        period = month_days(year, month)
        total = period_total(iem.days(station.cli_id, period[0], period[-1]), period)
        floor, cap = _dec(m.get("floor_strike")), _dec(m.get("cap_strike"))
        checks.append(
            SettlementCheck(
                ticker=m["ticker"],
                event_ticker=m["event_ticker"],
                strike_type=m.get("strike_type") or "",
                floor_strike=floor,
                cap_strike=cap,
                kalshi_result=m.get("result") or "",
                total=total,
                computed=resolve(total, m.get("strike_type") or "", floor, cap),
            )
        )
    return sorted(checks, key=lambda c: (c.event_ticker, c.floor_strike or Decimal(0)))


def format_checks(checks: list[SettlementCheck], series: str, station_id: str) -> str:
    lines = [f"Settlement replication: {series} from NWS CLI {station_id} (via IEM)"]
    event = ""
    for c in checks:
        if c.event_ticker != event:
            event = c.event_ticker
            t = c.total
            shown = "T" if t.inches == 0 and t.any_trace else f"{t.inches}"
            lines.append(
                f"\n{event}: CLI total {shown} in over {t.n_days} days"
                + (f", {t.n_missing} MISSING" if t.n_missing else "")
            )
        strike = c.floor_strike if c.floor_strike is not None else c.cap_strike
        ours = {True: "yes", False: "no", None: "undecided"}[c.computed]
        flag = "ok" if c.match else "MISMATCH"
        lines.append(
            f"  {c.ticker:<28} {c.strike_type:<8} {strike!s:>5}  "
            f"kalshi={c.kalshi_result:<4} ours={ours:<9} {flag}"
        )
    n_ok = sum(c.match for c in checks)
    lines.append(f"\n{n_ok}/{len(checks)} markets reproduced")
    return "\n".join(lines)
