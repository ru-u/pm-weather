"""Every issued version of the NWS Daily Climate Report (CLI), from IEM's AFOS archive.

IEM's CLI JSON (`iem_cli.py`) keeps only the latest report per day. The AFOS text
archive keeps every product with its WMO header, so the intraday report (about
4 pm local, "VALID TODAY AS OF 0400 PM LOCAL TIME", snowfall so far today) can be
joined to a decision time by its issue time. Endpoint (public):

    https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py
        ?pil=CLINYC&sdate=YYYY-MM-DD&edate=YYYY-MM-DD&fmt=text&limit=9999

Products are separated by SOH (0x01) ... ETX (0x03). In each product:
  - WMO header "CDUS41 KOKX 252137" (day, hour, minute UTC; "CCA"/"COR" for a
    correction) and a local issuance line "437 PM EST SUN JAN 25 2026";
  - "...THE CENTRAL PARK NY CLIMATE SUMMARY FOR JANUARY 25 2026..." (climate day);
  - intraday reports say "VALID [TODAY] AS OF ..." (4-5 pm, or about 7 am in Texas);
    final reports (after midnight) label it "YESTERDAY";
  - values: a number, "T" (trace) or "MM" (missing), possibly followed by "R"
    (a record). An intraday value is not final (D-032).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

AFOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
_TZ_OFFSET_H = {"EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6,
                "PST": -8, "PDT": -7, "AKST": -9, "AKDT": -8, "HST": -10}  # fmt: skip
_MONTHS = {
    m: i + 1
    for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())  # noqa: SIM905
}
_FULL_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
     "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"])}  # fmt: skip
_WMO_RE = re.compile(r"^([A-Z]{4}\d{2}) ([A-Z]{4}) (\d{2})(\d{2})(\d{2})(?: ([A-Z]{3}))?\s*$")
_LOCAL_RE = re.compile(
    r"^(\d{1,4}) (AM|PM) ([A-Z]{3,4}) [A-Z]{3} ([A-Z]{3}) (\d{1,2}) (\d{4})\s*(?:\.\.\..*)?$"
)
_DAY_RE = re.compile(r"CLIMATE SUMMARY FOR ([A-Z]+) (\d{1,2}),? (\d{4})")
_VALUE_RE = re.compile(r"^\s*(TODAY|YESTERDAY)\s+(MM|M|T|\d+(?:\.\d+)?)")
# Intraday headers: "VALID TODAY AS OF 0400 PM", "VALID AS OF 0500 PM" (Boston and
# Providence 2021-24), "VALID AS OF 0740 AM" (Texas morning reports). Finals have none.
_VALID_AS_OF_RE = re.compile(r"\bVALID (TODAY )?AS OF\b")


@dataclass(frozen=True)
class Value:
    inches: Decimal | None  # None for trace or missing
    trace: bool = False
    missing: bool = False


@dataclass(frozen=True)
class CliProduct:
    pil: str
    office: str
    issue_utc: datetime
    correction: bool
    climate_date: date
    intraday: bool
    snow: Value
    precip: Value


def split_products(text: str) -> list[str]:
    return [p.strip("\x03\n ") for p in text.split("\x01") if p.strip("\x03\n ")]


def _value(token: str) -> Value:
    if token in ("MM", "M"):
        return Value(None, missing=True)
    if token == "T":
        return Value(None, trace=True)
    return Value(Decimal(token))


def _section_value(lines: list[str], header: str) -> Value:
    for i, line in enumerate(lines):
        if line.strip().startswith(header):
            for nxt in lines[i + 1 : i + 4]:
                m = _VALUE_RE.match(nxt)
                if m:
                    return _value(m.group(2))
            break
    return Value(None, missing=True)


def _issue_utc(wmo: re.Match[str], local: re.Match[str]) -> datetime:
    """WMO day/hour/minute, with month and year taken from the local issuance line.

    Corrections and late retransmissions keep the original local line but carry a
    later WMO time, which is when they became public, so the WMO time is used when
    it is up to 4 days after the local line. A WMO time more than 3 hours before
    it is an inconsistency and the product is rejected."""
    hhmm = local.group(1).zfill(4)
    hour = int(hhmm[:2]) % 12 + (12 if local.group(2) == "PM" else 0)
    approx = datetime(
        int(local.group(6)), _MONTHS[local.group(4)], int(local.group(5)),
        hour, int(hhmm[2:]), tzinfo=UTC,
    ) - timedelta(hours=_TZ_OFFSET_H[local.group(3)])  # fmt: skip
    day, hh, mm = int(wmo.group(3)), int(wmo.group(4)), int(wmo.group(5))
    best: datetime | None = None
    for shift in (-1, 0, 1):
        y, m = approx.year, approx.month + shift
        y, m = (y - 1, 12) if m == 0 else (y + 1, 1) if m == 13 else (y, m)
        try:
            cand = datetime(y, m, day, hh, mm, tzinfo=UTC)
        except ValueError:
            continue
        if not timedelta(hours=-3) <= cand - approx <= timedelta(days=4):
            continue
        if best is None or abs(cand - approx) < abs(best - approx):
            best = cand
    if best is None:
        raise ValueError(f"WMO time {wmo.group(0)!r} inconsistent with {local.group(0)!r}")
    return best


def parse_product(text: str) -> CliProduct:
    lines = text.split("\n")
    wmo = next((m for ln in lines[:6] if (m := _WMO_RE.match(ln.strip()))), None)
    local = next((m for ln in lines[:15] if (m := _LOCAL_RE.match(ln.strip()))), None)
    if wmo is None or local is None:
        raise ValueError("no WMO header or local issuance line")
    pil = next((ln.strip() for ln in lines[:6] if ln.strip().startswith("CLI")), "")
    day_m = next((m for ln in lines[:30] if (m := _DAY_RE.search(ln))), None)
    if day_m is None:
        raise ValueError("no climate summary date")
    climate_date = date(int(day_m.group(3)), _FULL_MONTHS[day_m.group(1)], int(day_m.group(2)))
    intraday = any(_VALID_AS_OF_RE.search(ln) for ln in lines[:30])
    return CliProduct(
        pil=pil,
        office=wmo.group(2),
        issue_utc=_issue_utc(wmo, local),
        correction=wmo.group(6) in ("CCA", "CCB", "COR"),
        climate_date=climate_date,
        intraday=intraday,
        snow=_section_value(lines, "SNOWFALL"),
        precip=_section_value(lines, "PRECIPITATION"),
    )


def parse_archive(text: str) -> tuple[list[CliProduct], int]:
    """All parseable products, and the count that could not be parsed."""
    out, bad = [], 0
    for p in split_products(text):
        try:
            out.append(parse_product(p))
        except (ValueError, KeyError):
            bad += 1
    return out, bad


# ---------- pull ----------


def month_path(root: Path, pil: str, first: date) -> Path:
    return root / pil / f"{first:%Y-%m}.txt"


def pull_month(client: httpx.Client, root: Path, pil: str, first: date, today: date) -> bool:
    """Save one month of products for a PIL (skips saved months that are over)."""
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    path = month_path(root, pil, first)
    if path.exists() and nxt <= today:
        return False
    params: dict[str, str | int] = {
        "pil": pil, "sdate": f"{first:%Y-%m-%d}", "edate": f"{nxt:%Y-%m-%d}",
        "fmt": "text", "limit": 9999,
    }  # fmt: skip
    for attempt in range(6):
        try:
            resp = client.get(AFOS_URL, params=params)
            if resp.status_code == 200:
                break
        except httpx.TransportError:
            pass
        time.sleep(min(60.0, 5.0 * 2**attempt))
    else:
        raise RuntimeError(f"IEM AFOS failed for {pil} {first:%Y-%m}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(resp.text)
    tmp.rename(path)
    return True


# ---------- table ----------


def build_table(root: Path, station_of: dict[str, str]) -> tuple[list[dict[str, object]], int]:
    """One row per parsed product across all saved months; returns (rows, unparsed)."""
    rows: list[dict[str, object]] = []
    bad = 0
    for pil, station in sorted(station_of.items()):
        for f in sorted((root / pil).glob("*.txt")):
            ps, b = parse_archive(f.read_text(errors="replace"))
            bad += b
            for p in ps:
                rows.append(
                    {
                        "station": station, "pil": p.pil or pil, "office": p.office,
                        "issue_utc": p.issue_utc, "correction": p.correction,
                        "climate_date": p.climate_date, "intraday": p.intraday,
                        "snow_in": None if p.snow.inches is None else float(p.snow.inches),
                        "snow_trace": p.snow.trace, "snow_missing": p.snow.missing,
                        "precip_in": None if p.precip.inches is None else float(p.precip.inches),
                        "precip_trace": p.precip.trace, "precip_missing": p.precip.missing,
                    }
                )  # fmt: skip
    return rows, bad
