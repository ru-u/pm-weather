"""Parser tests on real saved Kalshi responses (tests/fixtures, fetched 2026-11-14)."""

from datetime import UTC, datetime

import httpx
import pyarrow as pa

from pmw.logger import schemas
from pmw.markets.kalshi import KalshiPublic

from .conftest import load_fixture

NOW = datetime(2026, 11, 14, 6, 0, tzinfo=UTC)
IDS = {"event_ticker": "KXNYCSNOWM-26DEC", "series_ticker": "KXNYCSNOWM", "grp": "snow"}


def test_orderbook_row_top_of_book():
    item = load_fixture("kalshi_orderbooks_batch.json")["orderbooks"][0]
    row = schemas.orderbook_row(item["ticker"], item["orderbook_fp"], NOW, IDS, "base")
    # Bids arrive ascending, so the best bid is the last level on each side.
    assert row["yes_px"] == sorted(row["yes_px"])
    assert row["no_px"] == sorted(row["no_px"])
    assert row["best_yes_bid"] == row["yes_px"][-1]
    assert row["best_yes_ask"] == round(1 - row["no_px"][-1], 4)
    assert row["best_yes_bid"] < row["best_yes_ask"]  # the book is not crossed
    assert len(row["yes_px"]) == len(row["yes_qty"])
    assert row["exchange_ts"] is None
    assert row["schema_version"] == schemas.SCHEMA_VERSION


def test_orderbook_row_empty_book():
    row = schemas.orderbook_row("X", {"yes_dollars": [], "no_dollars": []}, NOW, IDS, "hot")
    assert row["best_yes_bid"] is None and row["best_yes_ask"] is None
    assert row["yes_px"] == [] and row["no_qty"] == []
    # An empty or missing book must still fit the schema.
    pa.Table.from_pylist(
        [row, schemas.orderbook_row("Y", {}, NOW, IDS, "hot")], schema=schemas.ORDERBOOK
    )


def test_one_sided_book():
    row = schemas.orderbook_row("X", {"yes_dollars": [["0.1000", "5.00"]]}, NOW, IDS, "base")
    assert row["best_yes_bid"] == 0.10 and row["best_yes_bid_qty"] == 5.0
    assert row["best_yes_ask"] is None


def test_trade_row():
    t = load_fixture("kalshi_trades.json")["trades"][0]
    row = schemas.trade_row(t, NOW, IDS)
    assert row["trade_id"] == t["trade_id"]
    assert row["exchange_ts"].tzinfo is not None
    assert abs(row["yes_price"] + row["no_price"] - 1.0) < 1e-9
    assert row["count"] > 0
    pa.Table.from_pylist([row], schema=schemas.TRADE)


def test_market_row_and_meta_row():
    m = load_fixture("kalshi_markets_by_tickers.json")["markets"][0]
    row = schemas.market_row(m, NOW, IDS)
    assert row["ticker"] == m["ticker"]
    assert 0 <= row["yes_bid"] <= row["yes_ask"] <= 1
    assert row["result"] is None  # still open: empty string becomes null
    pa.Table.from_pylist([row], schema=schemas.MARKET)

    series = {
        "ticker": "KXNYCSNOWM",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "settlement_sources": [{"name": "The Weather Company"}],
    }
    meta = schemas.market_meta_row(m, {"title": "Snow in NYC"}, series, NOW, "snow")
    assert meta["rules_primary"].startswith("If the total snowfall")
    assert meta["strike_type"] == "greater"
    pa.Table.from_pylist([meta], schema=schemas.MARKET_META)


def test_meta_hash_ignores_prices_but_catches_rule_changes():
    m = load_fixture("kalshi_markets_by_tickers.json")["markets"][0]
    series = {"ticker": "KXNYCSNOWM"}
    base = schemas.market_meta_row(m, {}, series, NOW, "snow")
    repriced = dict(
        m, yes_bid_dollars="0.0100", volume_fp="999999.00", updated_time="2027-01-01T00:00:00Z"
    )
    assert (
        schemas.market_meta_row(repriced, {}, series, NOW, "snow")["meta_sha256"]
        == (base["meta_sha256"])
    )
    changed = dict(m, rules_secondary=m["rules_secondary"] + " Amended.")
    new = schemas.market_meta_row(changed, {}, series, NOW, "snow")
    assert new["meta_sha256"] != base["meta_sha256"]
    assert new["rules_sha256"] != base["rules_sha256"]


def _client(handler) -> KalshiPublic:
    return KalshiPublic(
        "https://example.invalid/trade-api/v2",
        min_request_interval_s=0,
        transport=httpx.MockTransport(handler),
    )


def test_client_sends_no_auth_headers_and_batches_orderbooks():
    seen: list[httpx.Request] = []
    body = load_fixture("kalshi_orderbooks_batch.json")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    client = _client(handler)
    tickers = [f"T{i}" for i in range(250)]
    client.orderbooks(tickers)
    assert len(seen) == 3  # 100 + 100 + 50
    assert [len(r.url.params.get_list("tickers")) for r in seen] == [100, 100, 50]
    for r in seen:
        assert not any(h.lower().startswith("kalshi-access") for h in r.headers)
        assert "authorization" not in {h.lower() for h in r.headers}


def test_client_retries_429_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"code": "too_many_requests"}})
        return httpx.Response(200, json={"trades": [], "cursor": ""})

    client = _client(handler)
    client._get.retry.wait = lambda *_a, **_k: 0  # type: ignore[attr-defined]
    assert client.trades("X") == []
    assert calls["n"] == 2


def test_client_follows_cursor_pagination():
    pages = [
        {"markets": [{"ticker": "A"}], "cursor": "c1"},
        {"markets": [{"ticker": "B"}], "cursor": ""},
    ]
    cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursors.append(request.url.params.get("cursor"))
        return httpx.Response(200, json=pages[len(cursors) - 1])

    out = _client(handler).markets(series_ticker="S")
    assert [m["ticker"] for m in out] == ["A", "B"]
    assert cursors == [None, "c1"]
