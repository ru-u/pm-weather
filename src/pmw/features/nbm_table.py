"""Turn raw NBP station blocks into one tidy table of 24-hour forecast periods.

One row per (station, cycle, 24-hour period). `available_at` is the time the
cycle can be assumed public; every later join must use `available_at < t`,
never `cycle < t`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from pmw.weather.nbm_text import available_at, day_path, parse_nbp_block, read_day

log = logging.getLogger(__name__)

_TS = pa.timestamp("us", tz="UTC")
_PCTS = (10, 25, 50, 75, 90)
SCHEMA = pa.schema(
    [
        ("station", pa.string()),
        ("cycle", _TS),  # issue time of the NBM cycle
        ("last_modified", _TS),  # S3 upload time of the bulletin file
        ("available_at", _TS),  # when the cycle is assumed public (see nbm_text.available_at)
        ("nbm_version", pa.string()),
        ("fhr", pa.int16()),
        ("valid_end", _TS),  # end of the 24-hour period (12Z)
        *[(f"snow_p{p}", pa.float32()) for p in _PCTS],  # inches
        *[(f"qpf_p{p}", pa.float32()) for p in _PCTS],  # inches of liquid
        *[(f"ice_p{p}", pa.float32()) for p in _PCTS],  # inches
    ]
)


def rows_for_day(root: Path, day: date) -> tuple[list[dict[str, Any]], int]:
    """Parsed rows for one UTC day of cycles, and the number of blocks that failed."""
    rows: list[dict[str, Any]] = []
    failed = 0
    for rec in read_day(root, "nbp", day):
        text = rec.get("text")
        if not isinstance(text, str):
            continue
        try:
            block = parse_nbp_block(text)
        except ValueError as exc:
            failed += 1
            log.warning(
                "unparseable NBP block %s %s: %s", rec.get("station"), rec.get("cycle"), exc
            )
            continue
        lm_raw = rec.get("last_modified")
        lm = datetime.fromisoformat(lm_raw) if isinstance(lm_raw, str) else None
        for p in block.periods:
            row: dict[str, Any] = {
                "station": block.station,
                "cycle": block.issue,
                "last_modified": lm,
                "available_at": available_at(block.issue, lm),
                "nbm_version": block.version,
                "fhr": p.fhr,
                "valid_end": p.valid_end,
            }
            for q in _PCTS:
                row[f"snow_p{q}"] = p.snow_in[q]
                row[f"qpf_p{q}"] = p.qpf_in[q]
                row[f"ice_p{q}"] = p.ice_in[q]
            rows.append(row)
    return rows, failed


def build_table(root: Path, days: list[date], out: Path) -> dict[str, int]:
    """Parse every pulled day into one Parquet file. Returns counts for a sanity check."""
    rows: list[dict[str, Any]] = []
    stats = {"days_present": 0, "days_absent": 0, "rows": 0, "failed_blocks": 0}
    for day in days:
        if not day_path(root, "nbp", day).exists():
            stats["days_absent"] += 1
            continue
        stats["days_present"] += 1
        day_rows, failed = rows_for_day(root, day)
        rows.extend(day_rows)
        stats["failed_blocks"] += failed
    stats["rows"] = len(rows)
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out, compression="zstd")
    return stats
