"""P2A-S: sample from logged metadata, quotes from logged books, book-walk taker,
gate, and the registered known-answer runs on synthetic books."""

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from pmw.eval import p2as
from pmw.eval.p2a import MarketSpec
from pmw.eval.p2a_score import score_all, synthetic_world
from pmw.utils.config import load_stations_config

STATIONS = load_stations_config()


def _meta(ticker, city, icao, opened, *, strike=1.0, strike_type="greater",
          series="KXSNOWCITIES", event="KXSNOWCITIES-27JAN10", rules=None):  # fmt: skip
    return {
        "received_at_utc": pd.Timestamp("2027-01-08T12:00", tz="UTC"),
        "ticker": ticker, "event_ticker": event, "series_ticker": series, "grp": "snow_storm",
        "strike_type": strike_type, "floor_strike": strike, "cap_strike": float("nan"),
        "open_time": pd.Timestamp(opened), "close_time": pd.Timestamp("2027-01-11T04:59", tz="UTC"),
        "rules_primary": rules or (f"If the total snowfall recorded in {city} ({icao}) from Jan 8 "
                                   f"to 10, 2027 is strictly greater than {strike:g} inch, then "
                                   "the market resolves to Yes."),
        "rules_secondary": "",
    }  # fmt: skip


def test_sample_groups_multi_city_events_by_station_and_records_exclusions():
    opened = "2027-01-05T03:00Z"
    meta = pd.DataFrame([
        _meta("KXSNOWCITIES-27JAN10-NYC", "New York City", "KNYC", opened),
        _meta("KXSNOWCITIES-27JAN10-BOS", "Boston", "KBOS", opened),
        _meta("KXSNOWCITIES-27JAN10-XXX", "Nowhere", "KZZZ", opened),
        _meta("KXSNOWSTORM-26NOVNYC-2", "New York City", "KNYC", "2026-11-10T03:00Z",
              series="KXSNOWSTORM", event="KXSNOWSTORM-26NOVNYC"),
        _meta("KXSNOWSTORM-27JANDC-B", "Washington DC", "KDCA", opened, strike_type="between",
              series="KXSNOWSTORM", event="KXSNOWSTORM-27JANDC"),
    ])  # fmt: skip
    events, excluded = p2as.sample_from_meta(meta, STATIONS)
    assert [(e.event_ticker, e.station.icao) for e in events] == [
        ("KXSNOWCITIES-27JAN10", "KBOS"), ("KXSNOWCITIES-27JAN10", "KNYC")]  # fmt: skip
    assert events[0].window == [date(2027, 1, 8), date(2027, 1, 9), date(2027, 1, 10)]
    assert events[0].kind == "storm"
    assert {x.ticker: x.reason for x in excluded} == {
        "KXSNOWCITIES-27JAN10-XXX": "station_not_configured",
        "KXSNOWSTORM-26NOVNYC-2": "opened_before_logger",
        "KXSNOWSTORM-27JANDC-B": "not_a_greater_strike",
    }
    dev_events, _ = p2as.sample_from_meta(meta, STATIONS, dev=True)
    assert [m.ticker for e in dev_events for m in e.markets] == ["KXSNOWSTORM-26NOVNYC-2"]
    ends = p2as.window_ends(events)
    assert list(ends) == ["KXSNOWCITIES-27JAN10"]  # one block key per event
    assert ends["KXSNOWCITIES-27JAN10"] == datetime(2027, 1, 11, 5, tzinfo=UTC)


def test_latest_metadata_row_wins():
    a = _meta("KXSNOWCITIES-27JAN10-NYC", "New York City", "KNYC", "2027-01-05T03:00Z", strike=1)
    b = {**a, "received_at_utc": a["received_at_utc"] + pd.Timedelta(hours=1), "floor_strike": 2.0}
    events, _ = p2as.sample_from_meta(pd.DataFrame([b, a]), STATIONS)
    assert events[0].markets[0].strike == 2.0


def _book(ticker, at, bid, ask, depth=100.0):
    return {"ticker": ticker, "received_at_utc": pd.Timestamp(at), "best_yes_bid": bid,
            "best_yes_ask": ask, "yes_px": [bid] if bid == bid else [], "yes_qty": [depth],
            "no_px": [round(1 - ask, 4)], "no_qty": [depth]}  # fmt: skip


def test_quote_is_latest_book_at_or_before_t_within_ten_minutes():
    t = pd.Timestamp("2027-01-09T14:30Z")
    f = pd.DataFrame({"ticker": ["A", "B", "C"], "t": [t, t, t]})
    books = pd.DataFrame([
        _book("A", "2027-01-09T14:20Z", 0.30, 0.34), _book("A", "2027-01-09T14:25Z", 0.31, 0.35),
        _book("A", "2027-01-09T14:31Z", 0.90, 0.95),  # after t: never used
        _book("B", "2027-01-09T14:19Z", 0.30, 0.34),  # 11 minutes old: no quote
        _book("C", "2027-01-09T14:30Z", 0.10, 0.12),  # exactly at t: used
    ])  # fmt: skip
    trades = pd.DataFrame({
        "ticker": ["A", "A", "A", "C"], "count": [5.0, 7.0, 100.0, 3.0],
        "exchange_ts": pd.to_datetime(["2027-01-09T13:00Z", "2027-01-09T13:59Z",
                                       "2027-01-09T14:05Z", "2027-01-09T12:59Z"]),
    })  # fmt: skip
    q = p2as.quotes_from_books(f, books, trades).set_index("ticker")
    assert set(q.index) == {"A", "C"}
    assert (q.loc["A", "bid"], q.loc["A", "ask"]) == (0.31, 0.35)
    assert q.loc["A", "candle_end"] == pd.Timestamp("2027-01-09T14:00Z")
    assert q.loc["A", "volume"] == 12.0  # 13:00 to 14:00, as P2A's 14:00 candle
    assert q.loc["C", "volume"] == 0.0


def _obs(rows):
    base = {"kind": "storm", "week": "2027-W02", "y": 1, "volume": 10_000.0}
    df = pd.DataFrame([{**base, **r} for r in rows])
    df["t"] = pd.to_datetime(df["t"], utc=True)
    return df


def test_book_taker_walks_levels_and_respects_caps():
    t = "2027-01-09T14:30Z"
    # YES at 0.30 (100) and 0.40 (100) and 0.60 (500); p = 0.60: the 0.60 level has no edge.
    deep = {"no_px": [0.40, 0.60, 0.70], "no_qty": [500.0, 100.0, 100.0],
            "yes_px": [0.20], "yes_qty": [100.0]}  # fmt: skip
    obs = _obs([{"ticker": "A", "event_ticker": "E", "t": t, "p_nbm": 0.60, **deep}])
    tr = p2as.simulate_taker_books(obs, tau=0.10)
    assert tr["contracts"].tolist() == [200.0]
    assert tr["levels"].tolist() == [2]
    assert tr["price"].iloc[0] == pytest.approx(0.35)
    assert tr["pnl"].iloc[0] == pytest.approx(200 * (1 - 0.35) - 200 * tr["fee_per"].iloc[0])
    # Volume share caps size; the event-day cap is shared across markets, best edge first.
    obs = _obs([
        {"ticker": "A", "event_ticker": "E", "t": t, "p_nbm": 0.60, "volume": 500.0, **deep},
        {"ticker": "B", "event_ticker": "E", "t": t, "p_nbm": 0.95, **deep},
    ])  # fmt: skip
    tr = p2as.simulate_taker_books(obs, tau=0.10).set_index("ticker")
    assert tr.loc["A", "contracts"] == 50.0
    assert tr.loc["B", "contracts"] == 250.0  # market-day cap
    # One position per market per UTC day.
    later = {"ticker": "A", "event_ticker": "E", "t": "2027-01-09T20:30Z", "p_nbm": 0.6, **deep}
    tr = p2as.simulate_taker_books(_obs([obs.iloc[0].to_dict() | {"t": t}, later]), tau=0.10)
    assert len(tr) == 1


def test_book_taker_buys_no_from_yes_bids():
    obs = _obs([{"ticker": "A", "event_ticker": "E", "t": "2027-01-09T14:30Z", "p_nbm": 0.10,
                 "y": 0, "no_px": [0.30], "no_qty": [50.0], "yes_px": [0.40, 0.50],
                 "yes_qty": [30.0, 20.0]}])  # fmt: skip
    tr = p2as.simulate_taker_books(obs, tau=0.10)
    assert tr["side"].tolist() == ["no"]
    assert tr["contracts"].tolist() == [50.0]  # NO at 0.50 (20) then 0.60 (30)
    assert tr["realized_edge"].iloc[0] == pytest.approx(1 - tr["price"].iloc[0])


def test_gate_and_date_guard():
    ev = p2as.EventSpec("E1", "KXSNOWSTORM", "storm", STATIONS.stations["KNYC"],
                        [date(2027, 1, 8)], [MarketSpec("M1", "greater", 1.0,
                                                        datetime(2027, 1, 5, tzinfo=UTC),
                                                        datetime(2027, 1, 9, tzinfo=UTC))])  # fmt: skip
    assert p2as.settled_coverage([ev], {"M1": 0}) == {"events": 1, "weeks": 1}
    assert p2as.settled_coverage([ev], {}) == {"events": 0, "weeks": 0}
    ok = {"events": 12, "weeks": 8}
    with pytest.raises(RuntimeError, match="on or after"):
        p2as.guard(ok, dev=False, today=date(2027, 5, 14))
    with pytest.raises(RuntimeError, match="needs 12"):
        p2as.guard({"events": 12, "weeks": 7}, dev=False, today=date(2027, 5, 15))
    p2as.guard(ok, dev=False, today=date(2027, 5, 15))
    p2as.guard({"events": 0, "weeks": 0}, dev=True, today=date(2026, 11, 17))


def _synthetic(nbm_noise, mkt_noise, seed):
    f, q, y, ends = synthetic_world(nbm_noise=nbm_noise, mkt_noise=mkt_noise, seed=seed)
    f["kind"] = "storm"
    books, trades = p2as.synthetic_books(q)
    return f, q, p2as.quotes_from_books(f, books, trades), y, ends


def test_synthetic_books_reproduce_p2a_quotes_and_brier():
    f, candles, quotes, y, ends = _synthetic(0.0, 1.2, 3)
    assert len(quotes) == len(candles)
    a = score_all(f, candles, y, ends, n_boot=200)
    b = p2as.score(f, quotes, y, ends, n_boot=200)
    assert b["primary"] == a["primary"]  # same observations, same D and CI
    assert b["taker"]["0.1"]["trades"] > 0


def test_synthetic_nbm_is_truth_market_is_noisy_gives_go():
    f, _, quotes, y, ends = _synthetic(0.0, 1.2, 3)
    r = p2as.score(f, quotes, y, ends, n_boot=2000)
    assert r["primary"]["ci_low"] > 0
    assert r["verdict"] == "GO"


def test_synthetic_market_is_truth_nbm_is_noisy_gives_no_go():
    f, _, quotes, y, ends = _synthetic(1.2, 0.0, 4)
    r = p2as.score(f, quotes, y, ends, n_boot=2000)
    assert r["primary"]["ci_high"] < 0
    assert r["verdict"] == "NO GO"


def test_no_books_means_no_quotes():
    f = pd.DataFrame({"ticker": ["A"], "t": [pd.Timestamp("2027-01-09T14:30Z")]})
    assert p2as.quotes_from_books(f, pd.DataFrame(), pd.DataFrame()).empty
    assert p2as.hour_volume(pd.DataFrame(), pd.Series([], dtype="datetime64[ns, UTC]"),
                            pd.Series([], dtype=str)).size == 0  # fmt: skip
    assert timedelta(minutes=10) == p2as.QUOTE_WITHIN


def test_nothing_usable_returns_counts_not_a_crash():
    f, candles, _, y, ends = _synthetic(0.0, 1.2, 3)
    one_sided = candles.assign(bid=float("nan"))
    books, trades = p2as.synthetic_books(one_sided)
    quotes = p2as.quotes_from_books(f, books, trades)
    r = p2as.score(f, quotes, y, ends, n_boot=100)
    assert r["verdict"] is None
    assert r["quote_counts"]["with_book"] == len(f)
    assert r["quote_counts"]["two_sided"] == 0
