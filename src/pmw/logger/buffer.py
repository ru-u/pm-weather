"""In-memory row buffers that flush to append-only Parquet, plus the raw sink.

Layout under the storage root:
    live/<source>/<table>/date=YYYY-MM-DD/part-<HHMMSS>-<seq>.parquet
    raw/kalshi/<kind>/date=YYYY-MM-DD/<HHMMSS>-<seq>.jsonl.gz

Files are never rewritten. A crash loses at most one flush interval of rows;
trades can be re-pulled from the API afterwards, orderbook snapshots cannot.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa

from pmw.logger.schemas import TABLES
from pmw.utils.storage import Storage
from pmw.utils.timeutil import utcnow

log = logging.getLogger(__name__)


class TableBuffers:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._rows: dict[str, list[dict[str, Any]]] = {name: [] for name in TABLES}
        self._seq = 0
        self._tag = f"{os.getpid():05d}"
        self.bytes_written: dict[str, int] = {name: 0 for name in TABLES}
        self.rows_written: dict[str, int] = {name: 0 for name in TABLES}

    def add(self, table: str, row: dict[str, Any]) -> None:
        self._rows[table].append(row)

    def extend(self, table: str, rows: list[dict[str, Any]]) -> None:
        self._rows[table].extend(rows)

    def pending_rows(self) -> int:
        return sum(len(r) for r in self._rows.values())

    def flush(self) -> dict[str, int]:
        """Write every non-empty buffer. Returns bytes written per table this flush."""
        written: dict[str, int] = {}
        now = utcnow()
        self._seq += 1
        for name, rows in self._rows.items():
            if not rows:
                continue
            source, schema = TABLES[name]
            # Split by the UTC date each row was received, so partitions are exact.
            by_date: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                day = row["received_at_utc"].astimezone(UTC).strftime("%Y-%m-%d")
                by_date.setdefault(day, []).append(row)
            total = 0
            try:
                for day, day_rows in by_date.items():
                    table = pa.Table.from_pylist(day_rows, schema=schema)
                    rel = (
                        f"live/{source}/{name}/date={day}/"
                        f"part-{now:%H%M%S}-{self._tag}-{self._seq:06d}.parquet"
                    )
                    total += self.storage.write_parquet(rel, table)
            except Exception:
                # Keep the rows and try again next flush rather than drop data.
                log.exception("flush failed for %s (%d rows kept in memory)", name, len(rows))
                continue
            self.rows_written[name] += len(rows)
            self.bytes_written[name] += total
            written[name] = total
            self._rows[name] = []
        return written


class RawSink:
    """Keeps gzipped JSONL of raw API responses, so a parser bug can be re-parsed.

    Each line is {"received_at": ..., "path": ..., "params": ..., "body": ...}.
    Compression happens as lines arrive, so memory holds compressed bytes only.
    """

    def __init__(self, storage: Storage, enabled: bool = True) -> None:
        self.storage = storage
        self.enabled = enabled
        self._bufs: dict[str, tuple[io.BytesIO, gzip.GzipFile]] = {}
        self._seq = 0
        self._tag = f"{os.getpid():05d}"
        self.bytes_written = 0

    @staticmethod
    def _kind(path: str) -> str:
        if path.endswith(("/orderbooks", "/orderbook")):
            return "orderbooks"
        if path.endswith("/trades"):
            return "trades"
        if path == "/markets":
            return "markets"
        if path.startswith(("/series", "/events")):
            return "discovery"
        return "other"

    def __call__(self, path: str, params: dict[str, Any], received: datetime, body: Any) -> None:
        if not self.enabled:
            return
        kind = self._kind(path)
        if kind not in self._bufs:
            raw = io.BytesIO()
            self._bufs[kind] = (raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6))
        line = json.dumps(
            {
                "received_at": received.astimezone(UTC).isoformat(),
                "path": path,
                "params": params,
                "body": body,
            },
            separators=(",", ":"),
        )
        self._bufs[kind][1].write(line.encode() + b"\n")

    def flush(self) -> int:
        now = utcnow()
        self._seq += 1
        total = 0
        for kind, (raw, gz) in list(self._bufs.items()):
            gz.close()
            data = raw.getvalue()
            del self._bufs[kind]
            if not data:
                continue
            rel = (
                f"raw/kalshi/{kind}/date={now:%Y-%m-%d}/{now:%H%M%S}-{self._tag}-{self._seq:06d}"
                ".jsonl.gz"
            )
            try:
                total += self.storage.write_bytes(rel, data)
            except Exception:
                log.exception("raw flush failed for %s (%d bytes dropped)", kind, len(data))
        self.bytes_written += total
        return total
