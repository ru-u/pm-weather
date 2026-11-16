"""ERCOT jobs that run on the Railway `ercot-jobs` service (never on the laptop).

  probe     How far back each archive goes, how many documents, how big they
            are. Saves one real document per product under raw/ercot/probe/ so
            the parsers can be tested on a real file.
  backfill  Every posted document of a product in a posted-date range, saved
            VERBATIM (the zip ERCOT served) under raw/ercot/<emil>/<yyyy>/<mm>/,
            with a per-month manifest of docId and postDatetime. Resumable: a
            document already in storage is not downloaded again. Every posted
            version is kept, because Kalshi settles on the FIRST complete one.

Parsing runs separately (laptop, after `make pull-data`): no ERCOT call needed.
Nothing here prints or stores a credential.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pyarrow as pa

from pmw.energy.ercot import CENTRAL, ArchiveDoc, ErcotPublic
from pmw.utils.storage import Storage

log = logging.getLogger(__name__)

RAW_ROOT = "raw/ercot"
MANIFEST_SCHEMA = pa.schema(
    [
        ("emil", pa.string()),
        ("doc_id", pa.int64()),
        ("post_datetime_raw", pa.string()),
        ("post_datetime_utc", pa.timestamp("us", tz="UTC")),
        ("friendly_name", pa.string()),
        ("path", pa.string()),
        ("bytes", pa.int64()),
        ("fetched_at_utc", pa.timestamp("us", tz="UTC")),
    ]
)


def doc_path(doc: ArchiveDoc) -> str:
    local = doc.post_datetime_utc.astimezone(CENTRAL)
    return f"{RAW_ROOT}/{doc.emil}/{local:%Y}/{local:%m}/{doc.doc_id}.zip"


def manifest_path(emil: str, month: str) -> str:
    return f"{RAW_ROOT}/{emil}/manifest/{month}.parquet"


def month_starts(start: date, end: date) -> list[date]:
    out, d = [], date(start.year, start.month, 1)
    while d <= end:
        out.append(d)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


def _central(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=CENTRAL)


def probe(client: ErcotPublic, storage: Storage, emils: list[str]) -> dict[str, Any]:
    """Archive extent and document size per product. One real document saved per product."""
    out: dict[str, Any] = {}
    far_past, now = _central(date(2000, 1, 1)), datetime.now(UTC)
    for emil in emils:
        first_page, total_pages, total = client.archive_page(emil, far_past, now, page=1, size=1)
        last_page, _, _ = client.archive_page(emil, far_past, now, page=max(total_pages, 1), size=1)
        ends = sorted(first_page + last_page, key=lambda d: d.post_datetime_utc)
        info: dict[str, Any] = {"documents": total}
        if ends:
            info["oldest_post"] = ends[0].post_datetime_raw
            info["newest_post"] = ends[-1].post_datetime_raw
            sample = ends[-1]
            data = client.download(emil, [sample.doc_id])[sample.doc_id]
            path = f"{RAW_ROOT}/probe/{emil}_{sample.doc_id}.zip"
            storage.write_bytes(path, data)
            info.update(sample_doc=sample.doc_id, sample_bytes=len(data), sample_path=path)
            info["sample_name"] = sample.friendly_name
        out[emil] = info
        log.info("probe %s: %s", emil, info)
    storage.write_bytes(
        f"{RAW_ROOT}/probe/probe_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json",
        json.dumps(out, indent=1).encode(),
    )
    return out


def backfill(
    client: ErcotPublic,
    storage: Storage,
    emil: str,
    start: date,
    end: date,
    post_hours: set[int] | None = None,
) -> dict[str, int]:
    """Download every document of `emil` POSTED between start and end (Central dates).

    `post_hours` keeps only documents posted in those Central hours (for hourly
    forecast products, to bound storage). None keeps everything.
    """
    stats = {"listed": 0, "kept": 0, "downloaded": 0, "already_had": 0, "bytes": 0}
    for m0 in month_starts(start, end):
        m1 = date(m0.year + (m0.month == 12), m0.month % 12 + 1, 1) - timedelta(days=1)
        lo, hi = max(m0, start), min(m1, end)
        docs = client.archive(emil, _central(lo), _central(hi + timedelta(days=1)))
        stats["listed"] += len(docs)
        if post_hours is not None:
            docs = [d for d in docs if d.post_datetime_utc.astimezone(CENTRAL).hour in post_hours]
        stats["kept"] += len(docs)
        todo = [d for d in docs if not storage.exists(doc_path(d))]
        stats["already_had"] += len(docs) - len(todo)
        fetched = datetime.now(UTC)
        if todo:
            files = client.download(emil, [d.doc_id for d in todo])
            for d in todo:
                stats["bytes"] += storage.write_bytes(doc_path(d), files[d.doc_id])
            stats["downloaded"] += len(todo)
        _write_manifest(storage, emil, docs, fetched)
        log.info("backfill %s %s: %s", emil, f"{m0:%Y-%m}", stats)
    return stats


def _write_manifest(storage: Storage, emil: str, docs: list[ArchiveDoc], fetched: datetime) -> None:
    """Merge this listing into the month manifests (keyed by docId; first sighting kept)."""
    by_month: dict[str, list[ArchiveDoc]] = defaultdict(list)
    for d in docs:
        by_month[f"{d.post_datetime_utc.astimezone(CENTRAL):%Y-%m}"].append(d)
    for month, items in by_month.items():
        path = manifest_path(emil, month)
        rows: dict[int, dict[str, Any]] = {}
        if storage.exists(path):
            for r in storage.read_parquet(path).to_pylist():
                rows[r["doc_id"]] = r
        for d in items:
            rows.setdefault(
                d.doc_id,
                {
                    "emil": emil,
                    "doc_id": d.doc_id,
                    "post_datetime_raw": d.post_datetime_raw,
                    "post_datetime_utc": d.post_datetime_utc,
                    "friendly_name": d.friendly_name,
                    "path": doc_path(d),
                    "bytes": None,
                    "fetched_at_utc": fetched,
                },
            )
        table = pa.Table.from_pylist(
            sorted(rows.values(), key=lambda r: (r["post_datetime_utc"], r["doc_id"])),
            schema=MANIFEST_SCHEMA,
        )
        storage.write_parquet(path, table)
