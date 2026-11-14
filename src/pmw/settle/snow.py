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

Outcomes are computed from NWS CLI daily snowfall (via IEM). Where the CLI
report has no snowfall value for a day, the value comes from ACIS, the data
service behind the NWS climate pages (see pmw.weather.acis). Every result
records how many days were filled that way.

What defines a contract (period, station, strike) is read from the market's own
rule text wherever the text states it, not assumed from the ticker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pmw.markets.kalshi import KalshiPublic
from pmw.utils.config import Station, StationsConfig
from pmw.utils.timeutil import date_range, month_days
from pmw.weather.acis import Acis
from pmw.weather.iem_cli import CliDay, IemCli, SnowValue

_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]  # fmt: skip
_MONTHS = {name[:3].upper(): i for i, name in enumerate(_MONTH_NAMES, 1)}
_DATE_RE = re.compile(r"\b([A-Z][a-z]{2,8})\.? (\d{1,2})(?:st|nd|rd|th)?, (\d{4})")
_MONTH_YEAR_RE = re.compile(r"\bin ([A-Z][a-z]{2,8}) (\d{4})\b")
_STRIKE_RE = re.compile(r"strictly greater than ([0-9]+(?:\.[0-9]+)?)")
_CLI_RE = re.compile(r"\bCLI([A-Z]{3})\b")


@dataclass(frozen=True)
class PeriodTotal:
    """Snowfall over a period, keeping trace and missing-day information."""

    inches: Decimal  # sum of measurable daily values
    any_trace: bool
    n_days: int
    n_missing: int  # days with no value in any source
    n_filled: int = 0  # days taken from the fallback source

    @property
    def complete(self) -> bool:
        return self.n_missing == 0

    @property
    def shown(self) -> str:
        return "T" if self.inches == 0 and self.any_trace else f"{self.inches}"


def period_total(
    days: dict[date, CliDay],
    period: list[date],
    fallback: dict[date, SnowValue] | None = None,
) -> PeriodTotal:
    """Sum daily snowfall. `fallback` is used only where the CLI value is missing."""
    total = Decimal(0)
    any_trace = False
    missing = 0
    filled = 0
    for d in period:
        row = days.get(d)
        value = row.snow if row is not None else None
        if value is None or value.missing:
            value = fallback.get(d) if fallback else None
            if value is None or value.missing:
                missing += 1
                continue
            filled += 1
        any_trace = any_trace or value.trace
        total += value.measurable
    return PeriodTotal(total, any_trace, len(period), missing, filled)


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


def _month_number(name: str) -> int | None:
    key = name.lower()
    for i, full in enumerate(_MONTH_NAMES, 1):
        if full == key or (len(key) <= 4 and full.startswith(key[:3])):
            return i
    return None


def parse_period(rules_primary: str, event_ticker: str) -> list[date]:
    """The contract's days, from the rule text when it states dates.

    Handles "between Nov 8, 2021 and Nov 30, 2021", "from Jan 24, 2026 through
    Jan 26, 2026", "in Feb 21, 2026 - Feb 24, 2026" and "in December 2026".
    Falls back to the month in the event ticker.
    """
    found = []
    for name, day, year in _DATE_RE.findall(rules_primary):
        month = _month_number(name)
        if month is not None:
            found.append(date(int(year), month, int(day)))
    if len(found) >= 2:
        return date_range(found[0], found[1])
    m = _MONTH_YEAR_RE.search(rules_primary)
    if m:
        month = _month_number(m.group(1))
        if month is not None:
            return month_days(int(m.group(2)), month)
    return month_days(*parse_monthly_event(event_ticker))


def parse_strike(market: dict[str, Any]) -> tuple[str, Decimal | None, Decimal | None]:
    """(strike_type, floor, cap). Old markets lack the fields, so read the rule text."""
    strike_type = market.get("strike_type") or ""
    floor = market.get("floor_strike")
    cap = market.get("cap_strike")
    if strike_type and (floor is not None or cap is not None):
        return (
            strike_type,
            None if floor is None else Decimal(str(floor)),
            None if cap is None else Decimal(str(cap)),
        )
    m = _STRIKE_RE.search(market.get("rules_primary") or "")
    if m:
        return "greater", Decimal(m.group(1)), None
    raise ValueError(f"cannot read strike for {market.get('ticker')}")


def find_station(
    market: dict[str, Any], series_ticker: str, stations: StationsConfig
) -> Station | None:
    """Station named in the rule text (CLI product id), else the series default."""
    text = f"{market.get('rules_primary') or ''} {market.get('rules_secondary') or ''}"
    m = _CLI_RE.search(text)
    if m:
        product = f"CLI{m.group(1)}"
        for st in stations.stations.values():
            if st.cli_product == product:
                return st
        return None  # the rules name a station we have not configured: do not guess
    return stations.for_series(series_ticker)


def parse_expiration_value(text: str | None) -> SnowValue | None:
    """Kalshi's recorded settlement value ('7.1 inches', 'T', '0.2'), when it gives one."""
    if not text:
        return None
    cleaned = re.sub(r"(?i)\s*(inches|inch|in)\b\.?", "", text.strip()).strip().strip('"')
    if cleaned.upper() in ("T", "TRACE"):
        return SnowValue(None, trace=True)
    try:
        return SnowValue(Decimal(cleaned))
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class SettlementCheck:
    ticker: str
    event_ticker: str
    station: str
    period: tuple[date, date]
    strike_type: str
    floor_strike: Decimal | None
    cap_strike: Decimal | None
    kalshi_result: str  # "yes" / "no" / other
    kalshi_value: SnowValue | None  # Kalshi's recorded expiration value, if any
    total: PeriodTotal
    computed: bool | None

    @property
    def match(self) -> bool:
        if self.kalshi_result not in ("yes", "no") or self.computed is None:
            return False
        return (self.kalshi_result == "yes") == self.computed

    @property
    def value_agrees(self) -> bool | None:
        """Does our total equal Kalshi's recorded value? None when Kalshi gave none."""
        if self.kalshi_value is None or not self.total.complete:
            return None
        if self.kalshi_value.trace:
            return self.total.inches == 0 and self.total.any_trace
        return self.kalshi_value.inches == self.total.inches


def check_series(
    client: KalshiPublic,
    iem: IemCli,
    series_ticker: str,
    stations: StationsConfig,
    acis: Acis | None = None,
    markets: list[dict[str, Any]] | None = None,
) -> list[SettlementCheck]:
    """Recompute every settled market of a snow series from official daily data."""
    checks = []
    for m in markets if markets is not None else client.settled_markets(series_ticker):
        station = find_station(m, series_ticker, stations)
        if station is None:
            raise ValueError(f"no configured settlement station for {m['ticker']}")
        period = parse_period(m.get("rules_primary") or "", m["event_ticker"])
        cli = iem.days(station.cli_id, period[0], period[-1])
        fallback = None
        if acis is not None and any(d not in cli or cli[d].snow.missing for d in period):
            fallback = acis.snow(station.icao, period[0], period[-1])
        total = period_total(cli, period, fallback)
        strike_type, floor, cap = parse_strike(m)
        checks.append(
            SettlementCheck(
                ticker=m["ticker"],
                event_ticker=m["event_ticker"],
                station=station.cli_id,
                period=(period[0], period[-1]),
                strike_type=strike_type,
                floor_strike=floor,
                cap_strike=cap,
                kalshi_result=m.get("result") or "",
                kalshi_value=parse_expiration_value(m.get("expiration_value")),
                total=total,
                computed=resolve(total, strike_type, floor, cap),
            )
        )
    return sorted(checks, key=lambda c: (c.event_ticker, c.floor_strike or Decimal(0)))


def summarize(checks: list[SettlementCheck]) -> dict[str, int]:
    binary = [c for c in checks if c.kalshi_result in ("yes", "no")]
    return {
        "markets": len(checks),
        "reproduced": sum(c.match for c in checks),
        "contradicted": sum(c.computed is not None and not c.match for c in binary),
        "undecided": sum(c.computed is None for c in binary),
        "not_binary": len(checks) - len(binary),
        "used_fallback": sum(c.total.n_filled > 0 for c in checks),
        "value_disagrees": sum(c.value_agrees is False for c in checks),
    }


def format_checks(checks: list[SettlementCheck], series: str, verbose: bool = True) -> str:
    lines = [f"Settlement replication: {series} (NWS CLI via IEM, gaps filled from ACIS)"]
    event = ""
    for c in checks:
        if c.event_ticker != event:
            event = c.event_ticker
            t = c.total
            extra = ""
            if t.n_filled:
                extra += f", {t.n_filled} day(s) from ACIS"
            if t.n_missing:
                extra += f", {t.n_missing} day(s) MISSING"
            if c.kalshi_value is not None:
                kv = "T" if c.kalshi_value.trace else str(c.kalshi_value.inches)
                flag = "" if c.value_agrees is not False else "  <-- DIFFERS"
                extra += f"; Kalshi recorded {kv}{flag}"
            lines.append(
                f"\n{event}: {c.station} {c.period[0]} to {c.period[1]}: total {t.shown} in{extra}"
            )
        if verbose or not c.match:
            strike = c.floor_strike if c.floor_strike is not None else c.cap_strike
            ours = {True: "yes", False: "no", None: "undecided"}[c.computed]
            lines.append(
                f"  {c.ticker:<30} {c.strike_type:<8} {strike!s:>5}  "
                f"kalshi={c.kalshi_result:<6} ours={ours:<9} {'ok' if c.match else 'MISMATCH'}"
            )
    s = summarize(checks)
    lines.append(
        f"\n{s['reproduced']}/{s['markets']} reproduced, {s['contradicted']} contradicted, "
        f"{s['undecided']} undecided, {s['not_binary']} not yes/no, "
        f"{s['used_fallback']} used ACIS for at least one day"
    )
    return "\n".join(lines)
