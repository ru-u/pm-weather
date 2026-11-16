"""NWS Daily Climate Report (CLI) data, as parsed by the Iowa Environmental Mesonet.

Endpoint: https://mesonet.agron.iastate.edu/json/cli.py?station=KNYC&year=2026
One row per station per climate day. A climate day is midnight to midnight LOCAL
STANDARD time (see pmw.utils.timeutil).

Snowfall values as IEM returns them:
    a number      inches, one decimal
    "T"           trace: some snow fell, less than 0.1 inch
    "M" or null   missing

IEM keeps the LATEST CLI product issued for each day (the `product` field names
it). Kalshi's rule is "initial non-preliminary value", so a late NWS correction
can make this data differ from what a market settled on. Always record
`product` next to any value used for settlement work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx

IEM_CLI_URL = "https://mesonet.agron.iastate.edu/json/cli.py"
TRACE = "T"
MISSING = {"M", "MM", ""}


@dataclass(frozen=True)
class SnowValue:
    """One day's CLI snowfall. Exactly one of inches / trace / missing describes it."""

    inches: Decimal | None  # None when trace or missing
    trace: bool = False
    missing: bool = False

    @property
    def measurable(self) -> Decimal:
        """Inches counted toward a multi-day sum: trace and missing add nothing."""
        return self.inches if self.inches is not None else Decimal(0)


@dataclass(frozen=True)
class CliDay:
    station: str
    valid: date
    snow: SnowValue
    snow_month: SnowValue  # NWS's own month-to-date total as printed in that day's report
    high: int | None
    low: int | None
    precip: SnowValue
    product: str  # NWS product id of the report IEM parsed, e.g. 202601021606-KOKX-CDUS41-CLINYC

    @property
    def issued_utc(self) -> datetime | None:
        """Issue time of the parsed product, from its id (first 12 digits, UTC)."""
        m = re.match(r"(\d{12})-", self.product)
        if not m:
            return None
        return datetime.strptime(m.group(1), "%Y%m%d%H%M").replace(tzinfo=UTC)


def parse_amount(value: Any) -> SnowValue:
    """Parse a CLI snowfall or precipitation field."""
    if value is None:
        return SnowValue(None, missing=True)
    if isinstance(value, str):
        text = value.strip().upper()
        if text == TRACE:
            return SnowValue(None, trace=True)
        if text in MISSING:
            return SnowValue(None, missing=True)
        return SnowValue(Decimal(text))
    return SnowValue(Decimal(str(value)))


def _int(value: Any) -> int | None:
    if value is None or (isinstance(value, str) and value.strip().upper() in MISSING):
        return None
    return int(value)


def parse_cli_json(payload: dict[str, Any]) -> list[CliDay]:
    days = []
    for r in payload.get("results") or []:
        days.append(
            CliDay(
                station=r["station"],
                valid=date.fromisoformat(r["valid"]),
                snow=parse_amount(r.get("snow")),
                snow_month=parse_amount(r.get("snow_month")),
                high=_int(r.get("high")),
                low=_int(r.get("low")),
                precip=parse_amount(r.get("precip")),
                product=r.get("product") or "",
            )
        )
    return sorted(days, key=lambda d: d.valid)


class IemCli:
    def __init__(self, timeout_s: float = 60.0, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            timeout=timeout_s,
            headers={"User-Agent": "pmw-research/0.1"},
            transport=transport,
        )
        self._cache: dict[tuple[str, int], list[CliDay]] = {}

    def year(self, station: str, year: int) -> list[CliDay]:
        key = (station, year)
        if key not in self._cache:
            resp = self._client.get(IEM_CLI_URL, params={"station": station, "year": year})
            resp.raise_for_status()
            self._cache[key] = parse_cli_json(resp.json())
        return self._cache[key]

    def days(self, station: str, start: date, end: date) -> dict[date, CliDay]:
        """CLI rows for start..end inclusive, keyed by climate day. Missing days are absent."""
        out: dict[date, CliDay] = {}
        for y in range(start.year, end.year + 1):
            for d in self.year(station, y):
                if start <= d.valid <= end:
                    out[d.valid] = d
        return out
