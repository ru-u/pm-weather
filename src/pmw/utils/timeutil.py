"""Time handling. Everything is stored in UTC; conversion is for display only.

The one non-obvious rule lives here: NWS climate days run on LOCAL STANDARD TIME
all year. A station at UTC-5 standard time has a climate day from 05:00Z to 05:00Z
whether or not daylight saving is in effect, which during DST is 1:00 AM to
12:59 AM on the local wall clock.
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, time, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (Kalshi style, with a trailing Z) to aware UTC."""
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Python accepts at most 6 fractional digits; Kalshi sometimes sends more.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        rest = ""
        for i, ch in enumerate(tail):
            if not ch.isdigit():
                rest = tail[i:]
                break
            digits += ch
        text = f"{head}.{digits[:6]}{rest}"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp has no timezone: {value!r}")
    return parsed.astimezone(UTC)


def climate_day_bounds_utc(day: date, std_utc_offset_hours: float) -> tuple[datetime, datetime]:
    """UTC [start, end) of the NWS climate day `day` for a station.

    `std_utc_offset_hours` is the station's STANDARD time offset (e.g. -5 for
    Eastern, -6 for Central). It is never the daylight offset.
    """
    start_local = datetime.combine(day, time(0, 0), tzinfo=UTC)
    start = start_local - timedelta(hours=std_utc_offset_hours)
    return start, start + timedelta(days=1)


def climate_date_for(ts_utc: datetime, std_utc_offset_hours: float) -> date:
    """The climate day that a UTC instant belongs to at a station."""
    if ts_utc.tzinfo is None:
        raise ValueError("ts_utc must be timezone-aware")
    return (ts_utc.astimezone(UTC) + timedelta(hours=std_utc_offset_hours)).date()


def month_days(year: int, month: int) -> list[date]:
    n = calendar.monthrange(year, month)[1]
    return [date(year, month, d) for d in range(1, n + 1)]


def date_range(start: date, end: date) -> list[date]:
    """Inclusive list of dates from start to end."""
    if end < start:
        raise ValueError("end before start")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]
