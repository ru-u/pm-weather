"""Buffers, storage layout, gap detection and the report, on a temp directory."""

from datetime import UTC, date, datetime, timedelta

from pmw.logger import schemas
from pmw.logger.buffer import RawSink, TableBuffers
from pmw.logger.report import build_report, find_gaps, format_report, prune_raw
from pmw.utils.storage import Storage

IDS = {"event_ticker": "E", "series_ticker": "S", "grp": "snow"}
BOOK = {"yes_dollars": [["0.4000", "10.00"]], "no_dollars": [["0.5500", "3.00"]]}


def _fill(buffers: TableBuffers, times: list[datetime]) -> None:
    for t in times:
        buffers.add("orderbooks", schemas.orderbook_row("S-1", BOOK, t, IDS, "base"))


def test_flush_partitions_by_received_date_and_round_trips(tmp_path):
    storage = Storage.local(tmp_path)
    buffers = TableBuffers(storage)
    _fill(
        buffers,
        [datetime(2026, 11, 14, 23, 55, tzinfo=UTC), datetime(2026, 11, 15, 0, 0, tzinfo=UTC)],
    )
    written = buffers.flush()
    assert written["orderbooks"] > 0 and buffers.pending_rows() == 0
    paths = [f.path for f in storage.list("live")]
    assert any("orderbooks/date=2026-11-14/" in p for p in paths)
    assert any("orderbooks/date=2026-11-15/" in p for p in paths)
    table = storage.read_parquet(next(p for p in paths if "2026-11-14" in p))
    assert table.schema.equals(schemas.ORDERBOOK)
    row = table.to_pylist()[0]
    assert row["yes_px"] == [0.4] and row["best_yes_ask"] == 0.45
    assert row["received_at_utc"] == datetime(2026, 11, 14, 23, 55, tzinfo=UTC)


def test_flush_is_append_only(tmp_path):
    storage = Storage.local(tmp_path)
    buffers = TableBuffers(storage)
    t = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    _fill(buffers, [t])
    buffers.flush()
    _fill(buffers, [t + timedelta(minutes=5)])
    buffers.flush()
    assert len(storage.list("live/kalshi/orderbooks")) == 2  # a new file each flush


def test_raw_sink_round_trip(tmp_path):
    import gzip
    import json

    storage = Storage.local(tmp_path)
    raw = RawSink(storage)
    raw("/markets/orderbooks", {"tickers": ["A"]}, datetime(2026, 11, 14, tzinfo=UTC), {"k": 1})
    raw("/markets/trades", {"ticker": "A"}, datetime(2026, 11, 14, tzinfo=UTC), {"trades": []})
    assert raw.flush() > 0
    files = storage.list("raw")
    assert {f.path.split("/")[2] for f in files} == {"orderbooks", "trades"}
    line = gzip.decompress(storage.read_bytes(files[0].path)).decode().splitlines()[0]
    assert json.loads(line)["body"] == {"k": 1}


def test_find_gaps():
    t0 = datetime(2026, 11, 14, 0, 0, tzinfo=UTC)
    times = [t0 + timedelta(minutes=m) for m in (0, 5, 10, 30, 35)]
    gaps = find_gaps(times, 300)
    assert len(gaps) == 1
    assert gaps[0][0] == t0 + timedelta(minutes=10) and gaps[0][2] == 1200
    assert find_gaps(times[:3], 300) == []


def test_report_counts_cycles_and_flags_gap(tmp_path):
    storage = Storage.local(tmp_path)
    buffers = TableBuffers(storage)
    t0 = datetime(2026, 11, 14, 0, 0, 2, tzinfo=UTC)
    _fill(buffers, [t0 + timedelta(minutes=m) for m in (0, 5, 10, 30)])
    trade = {
        "trade_id": "t1",
        "ticker": "S-1",
        "created_time": "2026-11-14T00:01:00Z",
        "yes_price_dollars": "0.4000",
        "no_price_dollars": "0.6000",
        "count_fp": "10.00",
    }
    buffers.add("trades", schemas.trade_row(trade, t0, IDS))
    buffers.add("trades", schemas.trade_row(trade, t0, IDS))  # duplicate print
    buffers.flush()
    r = build_report(storage, date(2026, 11, 14))
    assert r["orderbooks"]["base_cycles"] == 4
    assert len(r["orderbooks"]["gaps"]) == 1 and r["orderbooks"]["gaps"][0]["minutes"] == 20.0
    assert r["trades"]["unique_trades"] == 1
    assert r["trades"]["contracts_by_series"] == {"S": 10.0}
    assert "gaps: 1" in format_report(r)
    empty = build_report(storage, date(2026, 11, 13))
    assert empty["orderbooks"] is None and "none logged" in format_report(empty)


def test_prune_raw_only_removes_old_dates(tmp_path):
    storage = Storage.local(tmp_path)
    storage.write_bytes("raw/kalshi/orderbooks/date=2026-10-01/a.jsonl.gz", b"x")
    storage.write_bytes("raw/kalshi/orderbooks/date=2026-11-10/b.jsonl.gz", b"x")
    storage.write_bytes("live/kalshi/orderbooks/date=2026-10-01/p.parquet", b"x")
    assert prune_raw(storage, 14, date(2026, 11, 14)) == 1
    left = [f.path for f in storage.list("")]
    assert "raw/kalshi/orderbooks/date=2026-11-10/b.jsonl.gz" in left
    assert "live/kalshi/orderbooks/date=2026-10-01/p.parquet" in left  # live data is never pruned


def test_partition_date_is_utc_even_for_non_utc_timestamps(tmp_path):
    from datetime import timezone

    storage = Storage.local(tmp_path)
    buffers = TableBuffers(storage)
    eastern = timezone(timedelta(hours=-5))
    # 8 PM Eastern on Nov 14 is already Nov 15 in UTC.
    _fill(buffers, [datetime(2026, 11, 14, 20, 0, tzinfo=eastern)])
    buffers.flush()
    assert all("date=2026-11-15" in f.path for f in storage.list("live/kalshi/orderbooks"))
