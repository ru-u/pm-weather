"""Turn the raw ERCOT archive (pulled from the bucket) into analysis tables. Laptop only;
no ERCOT call is made here.

Inputs (verbatim documents saved by `pmw ercot-backfill`, with month manifests):
  raw/ercot/np6-346-cd/...   actual hourly load by forecast zone, every posted version
  raw/ercot/np3-565-cd/...   seven-day load forecasts, postings at selected CT hours

Outputs under data/interim/ercot/:
  np6_346_versions.parquet   one row per posted document: operating day, post time,
                             complete?, peak
  daily_peak.parquet         one row per operating day: the FIRST complete version's
                             peak (what Kalshi settles on), plus the latest version's
  hourly_load.parquet        hourly TOTAL and zones from the first complete version
  forecast_peaks.parquet     one row per (posting, delivery day, model): forecast daily
                             peak; model INUSE (in_use = True) is the hour-by-hour
                             flagged model, which is what P3A uses
  forecast_hourly.parquet    hourly SystemTotal of the in-use model, per posting
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from pmw.energy.ercot import (
    ACTUAL_LOAD_BY_FORECAST_ZONE,
    FZN_ZONES,
    IN_USE,
    LOAD_FORECAST_BY_WEATHER_ZONE,
    LoadHour,
    daily_peak,
    expected_hours,
    forecast_daily_peaks,
    in_use_daily_peaks,
    is_complete,
    parse_load_by_forecast_zone_csv,
    parse_load_forecast_csv,
    read_doc_csv,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Doc:
    doc_id: int
    post_utc: datetime
    path: Path


def manifest_docs(root: Path, emil: str) -> list[Doc]:
    """Documents listed in the month manifests that exist locally, in post order."""
    docs = []
    for mf in sorted((root / "raw/ercot" / emil / "manifest").glob("*.parquet")):
        for r in pq.read_table(mf).to_pylist():
            path = root / r["path"]
            if path.exists():
                docs.append(Doc(r["doc_id"], r["post_datetime_utc"], path))
            else:
                log.warning("manifest lists %s but it is not local", r["path"])
    return sorted({d.doc_id: d for d in docs}.values(), key=lambda d: (d.post_utc, d.doc_id))


def _f(x: Decimal | None) -> float | None:
    return None if x is None else float(x)


def build_actuals(root: Path, out: Path) -> dict[str, Any]:
    docs = manifest_docs(root, ACTUAL_LOAD_BY_FORECAST_ZONE)
    versions: list[dict[str, Any]] = []
    rows_by_doc: dict[int, list[LoadHour]] = {}
    by_day: dict[date, list[Doc]] = defaultdict(list)
    bad = 0
    for d in docs:
        try:
            _, text = read_doc_csv(d.path.read_bytes())
            rows = parse_load_by_forecast_zone_csv(text)
        except (ValueError, KeyError) as exc:
            bad += 1
            log.warning("doc %s unreadable: %s", d.doc_id, exc)
            continue
        rows_by_doc[d.doc_id] = rows
        for day in sorted({r.oper_day for r in rows}):
            complete = is_complete(rows, day)
            versions.append(
                {
                    "oper_day": day,
                    "doc_id": d.doc_id,
                    "post_utc": d.post_utc,
                    "n_rows": sum(r.oper_day == day for r in rows),
                    "expected_rows": expected_hours(day),
                    "complete": complete,
                    "peak": _f(daily_peak(rows, day))
                    if any(r.total for r in rows if r.oper_day == day)
                    else None,
                    "peak_str": str(daily_peak(rows, day))
                    if any(r.total for r in rows if r.oper_day == day)
                    else None,
                }
            )
            by_day[day].append(d)

    daily, hourly = [], []
    for day in sorted(by_day):
        vs = [v for v in versions if v["oper_day"] == day]
        vs.sort(key=lambda v: (v["post_utc"], v["doc_id"]))
        done = [v for v in vs if v["complete"]]
        first = done[0] if done else None
        latest = done[-1] if done else None
        daily.append(
            {
                "oper_day": day,
                "n_versions": len(vs),
                "first_posted_complete": vs[0]["complete"],
                "first_complete_doc": first["doc_id"] if first else None,
                "first_complete_post_utc": first["post_utc"] if first else None,
                "peak_first_complete": first["peak"] if first else None,
                "peak_first_complete_str": first["peak_str"] if first else None,
                "peak_latest": latest["peak"] if latest else None,
            }
        )
        if first:
            for r in rows_by_doc[first["doc_id"]]:
                if r.oper_day == day:
                    hourly.append(
                        {
                            "oper_day": day,
                            "hour_ending": r.hour_ending,
                            "repeated": r.repeated,
                            "total": _f(r.total),
                            **{z.lower(): _f(r.zones[z]) for z in FZN_ZONES},
                        }
                    )
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(versions), out / "np6_346_versions.parquet")
    pq.write_table(pa.Table.from_pylist(daily), out / "daily_peak.parquet")
    pq.write_table(pa.Table.from_pylist(hourly), out / "hourly_load.parquet")
    return {
        "documents": len(docs),
        "unreadable": bad,
        "operating_days": len(daily),
        "days_without_complete_version": sum(r["first_complete_doc"] is None for r in daily),
        "days_with_several_versions": sum(r["n_versions"] > 1 for r in daily),
        "days_first_posted_incomplete": sum(not r["first_posted_complete"] for r in daily),
        "days_latest_differs": sum(
            r["peak_latest"] is not None and r["peak_latest"] != r["peak_first_complete"]
            for r in daily
        ),
        "first_day": str(daily[0]["oper_day"]) if daily else None,
        "last_day": str(daily[-1]["oper_day"]) if daily else None,
    }


def build_forecasts(root: Path, out: Path) -> dict[str, Any]:
    docs = manifest_docs(root, LOAD_FORECAST_BY_WEATHER_ZONE)
    peaks_rows, hourly_rows = [], []
    bad = 0
    headers: defaultdict[str, int] = defaultdict(int)
    for d in docs:
        try:
            _, text = read_doc_csv(d.path.read_bytes())
            headers[text.split("\n", 1)[0].strip()] += 1
            rows = parse_load_forecast_csv(text)
        except (ValueError, KeyError) as exc:
            bad += 1
            log.warning("doc %s unreadable: %s", d.doc_id, exc)
            continue
        for day, peak in in_use_daily_peaks(rows).items():
            peaks_rows.append(
                {
                    "doc_id": d.doc_id,
                    "post_utc": d.post_utc,
                    "delivery_day": day,
                    "model": IN_USE,
                    "in_use": True,
                    "peak": float(peak),
                }
            )
        for (day, model), peak in forecast_daily_peaks(rows).items():
            peaks_rows.append(
                {
                    "doc_id": d.doc_id,
                    "post_utc": d.post_utc,
                    "delivery_day": day,
                    "model": model,
                    "in_use": False,  # single models; the in-use series is IN_USE
                    "peak": float(peak),
                }
            )
        for r in rows:
            if r.in_use:
                hourly_rows.append(
                    {
                        "doc_id": d.doc_id,
                        "post_utc": d.post_utc,
                        "delivery_day": r.delivery_day,
                        "hour_ending": r.hour_ending,
                        "repeated": r.repeated,
                        "model": r.model,
                        "system_total": _f(r.system_total),
                    }
                )
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(peaks_rows), out / "forecast_peaks.parquet")
    pq.write_table(pa.Table.from_pylist(hourly_rows), out / "forecast_hourly.parquet")
    return {
        "documents": len(docs),
        "unreadable": bad,
        "header_variants": len(headers),
        "postings_without_in_use_model": len(docs)
        - bad
        - len({r["doc_id"] for r in peaks_rows if r["in_use"]}),
        "postings": len(docs) - bad,
        "forecast_peak_rows": len(peaks_rows),
    }
