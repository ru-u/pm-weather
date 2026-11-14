"""Daily snowfall from ACIS (Applied Climate Information System, the RCCs).

ACIS is the data service behind the NWS "NOWData" climate pages, so it is the
same official record the Daily Climate Report draws on, served per station-day.
We use it as the SECOND source for snowfall: only for days where the CLI report
(via IEM) has no snowfall value.

Endpoint: POST https://data.rcc-acis.org/StnData
Values: a number string ("0.0", "11.4"), "T" for trace, "M" for missing. A
trailing letter flag (for example "1.2A") can appear on accumulated values.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from pmw.weather.iem_cli import SnowValue

ACIS_URL = "https://data.rcc-acis.org/StnData"


def parse_acis_value(value: Any) -> SnowValue:
    if value is None:
        return SnowValue(None, missing=True)
    text = str(value).strip().upper()
    if text == "T":
        return SnowValue(None, trace=True)
    if text in ("M", ""):
        return SnowValue(None, missing=True)
    try:
        return SnowValue(Decimal(text))
    except InvalidOperation:
        # Flagged values such as "1.2A" (multi-day accumulation) are not a clean
        # daily observation. Treat as missing rather than guess.
        return SnowValue(None, missing=True)


def parse_acis_json(payload: dict[str, Any]) -> dict[date, SnowValue]:
    out: dict[date, SnowValue] = {}
    for row in payload.get("data") or []:
        out[date.fromisoformat(row[0])] = parse_acis_value(row[1])
    return out


class Acis:
    def __init__(self, timeout_s: float = 60.0, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            timeout=timeout_s, headers={"User-Agent": "pmw-research/0.1"}, transport=transport
        )
        self._cache: dict[tuple[str, date, date], dict[date, SnowValue]] = {}

    def snow(self, station: str, start: date, end: date) -> dict[date, SnowValue]:
        """Daily snowfall for start..end inclusive. `station` is an ICAO id such as KSEA."""
        key = (station, start, end)
        if key not in self._cache:
            resp = self._client.post(
                ACIS_URL,
                json={
                    "sid": station,
                    "sdate": start.isoformat(),
                    "edate": end.isoformat(),
                    "elems": [{"name": "snow"}],
                    "meta": ["name"],
                },
            )
            resp.raise_for_status()
            self._cache[key] = parse_acis_json(resp.json())
        return self._cache[key]
