"""Settlement replication for KXTXERCOTPEAKD (ERCOT daily peak demand).

Market rule text (identical across all 1,334 markets settled by 2026-11-15):

  "If the highest hourly total system load reported for the ERCOT grid on
  November 14, 2026 is above 47,000 MW, then the market resolves to Yes."
  "The Expiration Value is the highest value in the TOTAL column of the first
  complete 'Actual System Load by Forecast Zone' CSV report published by ERCOT
  for the November 14, 2026 operating day. ... Values will be compared as
  published without rounding."

So: outcome = first_complete(NP6-346-CD versions).peak > strike, strictly.
The event ticker carries the day AFTER the operating day
(KXTXERCOTPEAKD-26NOV15 is about Nov 14); the operating day is read from the
rule text and must agree with that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

_OPER_DAY_RE = re.compile(r"\bon ([A-Z][a-z]+)\.? (\d{1,2}), (\d{4})\b")
_TICKER_DAY_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})$")
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _month(name: str) -> int:
    return _MONTHS.index(name[:3].upper()) + 1


def event_date(event_ticker: str) -> date:
    """'KXTXERCOTPEAKD-26NOV15' -> 2026-11-15 (the day after the operating day)."""
    m = _TICKER_DAY_RE.search(event_ticker)
    if not m:
        raise ValueError(f"not a daily ERCOT event ticker: {event_ticker}")
    return date(2000 + int(m.group(1)), _month(m.group(2)), int(m.group(3)))


def operating_day(market: dict[str, Any]) -> date:
    """Operating day from the rule text, checked against the ticker date minus one."""
    m = _OPER_DAY_RE.search(market.get("rules_primary") or "")
    if not m:
        raise ValueError(f"no operating day in rules of {market.get('ticker')}")
    day = date(int(m.group(3)), _month(m.group(1)), int(m.group(2)))
    expected = event_date(market["event_ticker"]) - timedelta(days=1)
    if day != expected:
        raise ValueError(f"{market.get('ticker')}: rules say {day}, ticker implies {expected}")
    return day


def parse_mw(text: str | None) -> Decimal | None:
    """Kalshi expiration value ('81,227.32' or '78487.51') as published."""
    if not text:
        return None
    try:
        return Decimal(text.replace(",", "").replace("MW", "").strip())
    except InvalidOperation:
        return None


def resolve(peak: Decimal, strike_type: str, floor: Decimal | None, cap: Decimal | None) -> bool:
    if strike_type == "greater" and floor is not None:
        return peak > floor
    if strike_type == "less" and cap is not None:
        return peak < cap
    if strike_type == "between" and floor is not None and cap is not None:
        return floor <= peak <= cap
    raise ValueError(f"unsupported strike {strike_type!r} {floor} {cap}")


def values_agree(kalshi: Decimal, ours: Decimal) -> bool:
    """True if `ours`, cut or rounded to Kalshi's displayed decimals, equals `kalshi`."""
    if kalshi == ours:
        return True
    exp = kalshi.as_tuple().exponent
    places = -exp if isinstance(exp, int) and exp < 0 else 0
    q = Decimal(1).scaleb(-places)
    return kalshi in (
        ours.quantize(q, rounding=ROUND_DOWN),
        ours.quantize(q, rounding=ROUND_HALF_UP),
    )


@dataclass(frozen=True)
class PeakCheck:
    ticker: str
    oper_day: date
    strike_type: str
    floor: Decimal | None
    cap: Decimal | None
    kalshi_result: str
    kalshi_value: Decimal | None
    our_peak: Decimal | None
    computed: bool | None

    @property
    def match(self) -> bool:
        return self.computed is not None and self.kalshi_result == (
            "yes" if self.computed else "no"
        )

    @property
    def value_agrees(self) -> bool | None:
        """Equal at the precision Kalshi recorded.

        Kalshi's expiration_value is sometimes a shortened display of the published
        number: 5 of 78 days so far show it cut to whole MW (73917.77 -> "73917")
        or rounded to one decimal (85799.88 -> "85799.9"). Strikes are multiples of
        500 MW, so this cannot change an outcome; the outcome check is separate.
        """
        if self.kalshi_value is None or self.our_peak is None:
            return None
        return values_agree(self.kalshi_value, self.our_peak)


def check_markets(markets: list[dict[str, Any]], peaks: dict[date, Decimal]) -> list[PeakCheck]:
    """Recompute each market from `peaks` (operating day -> first-complete peak)."""
    out = []
    for m in markets:
        day = operating_day(m)
        floor = m.get("floor_strike")
        cap = m.get("cap_strike")
        floor_d = None if floor is None else Decimal(str(floor))
        cap_d = None if cap is None else Decimal(str(cap))
        peak = peaks.get(day)
        out.append(
            PeakCheck(
                ticker=m["ticker"],
                oper_day=day,
                strike_type=m.get("strike_type") or "",
                floor=floor_d,
                cap=cap_d,
                kalshi_result=m.get("result") or "",
                kalshi_value=parse_mw(m.get("expiration_value")),
                our_peak=peak,
                computed=None if peak is None else resolve(peak, m["strike_type"], floor_d, cap_d),
            )
        )
    return sorted(out, key=lambda c: (c.oper_day, c.floor or Decimal(0)))


def summarize(checks: list[PeakCheck]) -> dict[str, int]:
    days = {c.oper_day for c in checks}
    return {
        "markets": len(checks),
        "operating_days": len(days),
        "reproduced": sum(c.match for c in checks),
        "contradicted": sum(c.computed is not None and not c.match for c in checks),
        "no_data": sum(c.computed is None for c in checks),
        "days_value_differs": len({c.oper_day for c in checks if c.value_agrees is False}),
    }
