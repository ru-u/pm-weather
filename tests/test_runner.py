"""Logger loop logic against a fake Kalshi client (no network)."""

from datetime import UTC, datetime, timedelta
from typing import Any

from pmw.logger.runner import MarketLogger, _in_window, _next_boundary
from pmw.utils.config import load_logger_config, load_series_config
from pmw.utils.storage import Storage

NOW = datetime.now(UTC)


def _market(ticker: str, event: str, close_in_h: float, volume: float = 0.0) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "event_ticker": event,
        "status": "active",
        "title": event,
        "yes_sub_title": "Above 2 inches",
        "close_time": (NOW + timedelta(hours=close_in_h)).isoformat(),
        "updated_time": NOW.isoformat(),
        "volume_fp": f"{volume:.2f}",
        "volume_24h_fp": f"{volume:.2f}",
        "open_interest_fp": "0.00",
        "yes_bid_dollars": "0.4000",
        "yes_ask_dollars": "0.4500",
        "rules_primary": "If the total snowfall ...",
        "rules_secondary": "",
        "strike_type": "greater",
        "floor_strike": 2,
    }


class FakeKalshi:
    def __init__(self) -> None:
        self.request_count = 0
        self.volumes = {"KXNYCSNOWM-26DEC-2": 0.0, "KXSNOWSTORM-26NOVNYC-4": 50.0}
        self.trade_calls: list[tuple[str, int | None]] = []

    def series_list(self, category: str | None = None) -> list[dict[str, Any]]:
        if category != "Climate and Weather":
            return []
        return [
            {"ticker": "KXNYCSNOWM", "title": "NYC Snowfall monthly", "tags": ["Snow"]},
            {"ticker": "KXSNOWSTORM", "title": "Snowstorms", "tags": ["Snow"]},
            {"ticker": "KXRAINNYCM", "title": "Rain in NYC", "tags": ["Snow and rain"]},
        ]

    def events(self, series_ticker: str, **_: Any) -> list[dict[str, Any]]:
        if series_ticker == "KXNYCSNOWM":
            ev, tk, hours = "KXNYCSNOWM-26DEC", "KXNYCSNOWM-26DEC-2", 24 * 40
        else:
            ev, tk, hours = "KXSNOWSTORM-26NOVNYC", "KXSNOWSTORM-26NOVNYC-4", 30
        return [
            {
                "event_ticker": ev,
                "title": ev,
                "markets": [_market(tk, ev, hours, self.volumes[tk])],
            }
        ]

    def orderbooks(self, tickers: list[str]) -> list[tuple[str, dict[str, Any], datetime]]:
        book = {"yes_dollars": [["0.4000", "10.00"]], "no_dollars": [["0.5500", "5.00"]]}
        return [(t, book, datetime.now(UTC)) for t in tickers]

    def markets(self, *, tickers: list[str]) -> list[dict[str, Any]]:
        return [_market(t, t.rsplit("-", 1)[0], 30, self.volumes[t]) for t in tickers]

    def trades(self, ticker: str, *, min_ts: int | None = None) -> list[dict[str, Any]]:
        self.trade_calls.append((ticker, min_ts))
        return [
            {
                "trade_id": f"{ticker}-t1",
                "ticker": ticker,
                "created_time": NOW.isoformat(),
                "yes_price_dollars": "0.4000",
                "no_price_dollars": "0.6000",
                "count_fp": "50.00",
            }
        ]

    def close(self) -> None:
        pass


def _logger(tmp_path) -> tuple[MarketLogger, FakeKalshi]:
    fake = FakeKalshi()
    cfg = load_logger_config()
    cfg.bursts.windows_utc = []  # keep "hot" deterministic regardless of the clock
    logger = MarketLogger(cfg, load_series_config(), Storage.local(tmp_path), client=fake)  # type: ignore[arg-type]
    return logger, fake


def test_discovery_tracks_matches_and_versions_metadata_once(tmp_path):
    logger, _ = _logger(tmp_path)
    logger.run_discovery()
    assert set(logger.tracked) == {"KXNYCSNOWM-26DEC-2", "KXSNOWSTORM-26NOVNYC-4"}
    assert logger.tracked["KXSNOWSTORM-26NOVNYC-4"].grp == "snow_storm"
    assert len(logger.buffers._rows["market_meta"]) == 2
    logger.run_discovery()  # nothing changed: no new metadata versions
    assert len(logger.buffers._rows["market_meta"]) == 2
    assert len(logger.buffers._rows["series_meta"]) == 2


def test_base_cycle_logs_books_markets_and_queues_trades(tmp_path):
    logger, fake = _logger(tmp_path)
    logger.run_discovery()
    logger.base_cycle()
    assert len(logger.buffers._rows["orderbooks"]) == 2
    assert len(logger.buffers._rows["markets"]) == 2
    # Only the market that has traded in the last 24h gets a trade fetch.
    assert list(logger._trade_queue) == ["KXSNOWSTORM-26NOVNYC-4"]
    assert logger.drain_one_trade_fetch() is True
    assert logger.drain_one_trade_fetch() is False
    assert len(logger.buffers._rows["trades"]) == 1

    # Next cycle: same volume, so no new fetch. Then volume moves and it is re-pulled,
    # but the already-seen trade id is not written twice.
    logger.base_cycle()
    assert not logger._trade_queue
    fake.volumes["KXSNOWSTORM-26NOVNYC-4"] = 60.0
    logger.base_cycle()
    assert list(logger._trade_queue) == ["KXSNOWSTORM-26NOVNYC-4"]
    logger.drain_one_trade_fetch()
    assert len(logger.buffers._rows["trades"]) == 1
    assert len(fake.trade_calls) == 2

    logger.flush()
    files = [f.path for f in logger.storage.list("live")]
    assert any("/orderbooks/" in p for p in files) and any("/trades/" in p for p in files)
    assert any("/health/" in p for p in files)


def test_hot_set_storm_window_and_volume_spike(tmp_path):
    logger, fake = _logger(tmp_path)
    logger.run_discovery()
    hot, why = logger.hot_tickers(datetime.now(UTC))
    assert hot == ["KXSNOWSTORM-26NOVNYC-4"] and why == "hot"  # closes within 72h
    logger.base_cycle()
    fake.volumes["KXNYCSNOWM-26DEC-2"] = 500.0  # a 500-contract burst in the monthly
    logger.base_cycle()
    hot, _ = logger.hot_tickers(datetime.now(UTC))
    assert set(hot) == {"KXSNOWSTORM-26NOVNYC-4", "KXNYCSNOWM-26DEC-2"}
    later = datetime.now(UTC) + timedelta(seconds=logger.cfg.hot.volume_hot_hold_s + 60)
    hot, _ = logger.hot_tickers(later)
    assert "KXNYCSNOWM-26DEC-2" not in hot


def test_burst_window_makes_snow_markets_hot(tmp_path):
    logger, _ = _logger(tmp_path)
    logger.cfg.bursts.windows_utc = [("03:30", "05:15")]
    logger.run_discovery()
    hot, why = logger.hot_tickers(datetime(2026, 12, 1, 4, 0, tzinfo=UTC))
    assert "KXNYCSNOWM-26DEC-2" in hot and why == "burst"


def test_window_and_boundary_helpers():
    assert _in_window(datetime(2026, 1, 1, 3, 30, tzinfo=UTC), [("03:30", "05:15")])
    assert not _in_window(datetime(2026, 1, 1, 5, 15, tzinfo=UTC), [("03:30", "05:15")])
    assert _in_window(datetime(2026, 1, 1, 0, 10, tzinfo=UTC), [("23:30", "00:30")])  # wraps
    assert _next_boundary(1000.0, 300) == 1200
    assert _next_boundary(1200.0, 300) == 1500  # strictly after


def test_market_snapshot_keeps_batches_that_worked(tmp_path):
    logger, fake = _logger(tmp_path)
    logger.run_discovery()
    calls = {"n": 0}
    real = fake.markets

    def flaky(*, tickers):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Kalshi 429 on /markets")
        return real(tickers=tickers)

    fake.markets = flaky  # type: ignore[method-assign]
    import pmw.logger.runner as runner_mod

    runner_mod.MARKETS_BATCH, saved = 1, runner_mod.MARKETS_BATCH  # one ticker per batch
    try:
        assert logger.snapshot_markets() is True  # one batch failed, one worked
    finally:
        runner_mod.MARKETS_BATCH = saved
    assert len(logger.buffers._rows["markets"]) == 1
    health = [r for r in logger.buffers._rows["health"] if r["kind"] == "markets"][-1]
    assert health["n_ok"] == 1 and health["n_err"] == 1 and "429" in health["note"]
