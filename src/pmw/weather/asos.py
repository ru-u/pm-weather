"""ASOS station observations (METAR) via the Iowa Environmental Mesonet.

Endpoint (public, no key):
    https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py
    ?station=IAH&data=tmpf&year1=..&month1=..&day1=..&year2=..&month2=..&day2=..
    &tz=Etc/UTC&format=onlycomma&latlon=no&missing=M&report_type=3&report_type=4

`report_type=3` is routine hourly METARs (normally at :51 to :56), `4` adds
specials. Times are UTC with `tz=Etc/UTC`. The end date is exclusive.

Availability: a METAR reaches public feeds within minutes of its observation
time. `AVAILABILITY_LAG` (15 minutes) is added to every observation before it
may be joined to a decision time, which is later than the normal delay.

IEM asks automated users to make modest, sequential requests: one request per
station per year, run one at a time with a pause.
"""

from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
AVAILABILITY_LAG = timedelta(minutes=15)


@dataclass(frozen=True)
class Obs:
    station: str  # IEM id, e.g. "IAH"
    valid: datetime  # observation time, UTC
    tmpf: float  # deg F

    @property
    def available_at(self) -> datetime:
        return self.valid + AVAILABILITY_LAG


def asos_params(
    station: str, start: date, end_exclusive: date
) -> list[tuple[str, str | int | float | bool | None]]:
    return [
        ("station", station),
        ("data", "tmpf"),
        ("year1", start.year),
        ("month1", start.month),
        ("day1", start.day),
        ("year2", end_exclusive.year),
        ("month2", end_exclusive.month),
        ("day2", end_exclusive.day),
        ("tz", "Etc/UTC"),
        ("format", "onlycomma"),
        ("latlon", "no"),
        ("missing", "M"),
        ("report_type", 3),
        ("report_type", 4),
    ]


def parse_asos_csv(text: str) -> list[Obs]:
    """Rows with a numeric temperature; "M" (missing) rows are skipped."""
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not {"station", "valid", "tmpf"} <= set(reader.fieldnames):
        raise ValueError(f"unexpected ASOS header: {reader.fieldnames}")
    out: list[Obs] = []
    for row in reader:
        raw = (row["tmpf"] or "").strip()
        if raw in ("", "M"):
            continue
        valid = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        out.append(Obs(row["station"], valid, float(raw)))
    return out


def pull_year(
    client: httpx.Client, root: Path, station: str, year: int, *, today: date | None = None
) -> Path | None:
    """Save one station-year as CSV (skips years already saved; the current year is
    re-fetched so it stays fresh). Returns the path, or None when nothing was written."""
    today = today or datetime.now(UTC).date()
    path = root / station / f"{station}_{year}.csv"
    if path.exists() and year < today.year:
        return None
    start = date(year, 1, 1)
    end = min(date(year + 1, 1, 1), today + timedelta(days=1))
    for attempt in range(6):
        try:
            resp = client.get(ASOS_URL, params=asos_params(station, start, end))
            if resp.status_code == 200:
                break
        except httpx.TransportError:
            pass
        time.sleep(min(60.0, 5.0 * 2**attempt))
    else:
        raise RuntimeError(f"IEM ASOS failed for {station} {year}")
    parse_asos_csv(resp.text)  # refuse to save something that does not parse
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(resp.text)
    tmp.rename(path)
    return path


def fetch_text(client: httpx.Client, station: str, start: date, end_exclusive: date) -> str:
    """Raw IEM CSV for a short span (a live job fetches today and yesterday)."""
    resp = client.get(ASOS_URL, params=asos_params(station, start, end_exclusive))
    resp.raise_for_status()
    return resp.text
