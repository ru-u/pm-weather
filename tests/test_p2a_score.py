"""P2A scoring: unit checks and the registered synthetic known-answer runs."""

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from pmw.eval.p2a_score import (
    DiffResult,
    assemble,
    block_bootstrap_mean,
    brier,
    decide,
    logloss,
    murphy,
    score_all,
    simulate_taker,
    synthetic_world,
)


def test_murphy_decomposition_adds_up_for_binned_forecasts():
    rng = np.random.default_rng(0)
    p = rng.integers(0, 10, 20_000) / 10 + 0.05  # forecasts at bin centres
    y = (rng.random(20_000) < p).astype(float)
    m = murphy(p, y)
    assert brier(p, y).mean() == pytest.approx(
        m["reliability"] - m["resolution"] + m["uncertainty"], abs=1e-9
    )
    assert m["reliability"] < 0.001  # calibrated by construction


def test_logloss_is_clipped():
    assert np.isfinite(logloss(np.array([0.0, 1.0]), np.array([1.0, 0.0]))).all()


def test_block_bootstrap_keeps_blocks_together():
    vals = np.array([1.0, 1.0, 1.0, -1.0])
    blocks = np.array(["a", "a", "a", "b"])
    boot = block_bootstrap_mean(vals, blocks, n_boot=2000)
    # Only three outcomes are possible: all a (1.0), all b (-1.0), one each (0.5).
    assert set(np.round(np.unique(boot), 6)) <= {1.0, -1.0, 0.5}


def test_decision_rule():
    pos, neg, wide = (
        DiffResult(1, 1, 0.01, 0.002, 0.02),
        DiffResult(1, 1, -0.01, -0.02, -0.002),
        DiffResult(1, 1, 0.001, -0.01, 0.01),
    )
    assert decide(pos, 0.95, [pos, pos]) == "GO"
    assert decide(pos, 0.80, [pos, pos]) == "CONDITIONAL"  # P&L condition fails
    assert decide(neg, 0.20, [neg, neg]) == "NO GO"
    assert decide(neg, 0.60, [neg, neg]) == "CONDITIONAL"
    assert decide(wide, 0.99, [wide, wide]) == "CONDITIONAL"
    assert decide(pos, 0.99, [pos, neg]) == "CONDITIONAL"  # sensitivity flips the sign


def test_assemble_applies_registered_filters():
    t = pd.Timestamp(datetime(2026, 1, 5, 14, 30, tzinfo=UTC))
    f = pd.DataFrame([
        {"event_ticker": "E", "series": "S", "kind": "monthly", "station": "K", "ticker": tk, "strike": 1.0,
         "t": t, "p_nbm": 0.5, "p_nbm_indep": 0.5, "p_climo": 0.5, "determined": det, "nbm_versions": "V4.3"}
        for tk, det in (("A", False), ("B", False), ("C", True), ("D", False), ("E1", False))
    ])  # fmt: skip
    q = pd.DataFrame([
        {"ticker": "A", "candle_end": t.floor("h"), "bid": 0.40, "ask": 0.45, "volume": 10.0},  # usable
        {"ticker": "B", "candle_end": t.floor("h"), "bid": 0.10, "ask": 0.35, "volume": 10.0},  # spread 0.25
        {"ticker": "C", "candle_end": t.floor("h"), "bid": 0.40, "ask": 0.45, "volume": 10.0},  # determined
        {"ticker": "D", "candle_end": t.floor("h") - pd.Timedelta(hours=1), "bid": 0.4, "ask": 0.45, "volume": 1.0},  # stale hour
        {"ticker": "E1", "candle_end": t.floor("h"), "bid": 0.0, "ask": 0.05, "volume": 1.0},  # one-sided
    ])  # fmt: skip
    obs = assemble(f, q, {k: 1 for k in "ABCD"} | {"E1": 0}, {"E": t + pd.Timedelta(days=2)})
    assert list(obs["ticker"]) == ["A"]
    assert obs["p_mkt"].iloc[0] == pytest.approx(0.425) and obs["lead_band"].iloc[0] == "< 3 d"


def test_taker_respects_one_position_per_market_day_and_caps():
    t1 = pd.Timestamp(datetime(2026, 1, 5, 2, 30, tzinfo=UTC))
    rows = []
    for t in (t1, t1 + pd.Timedelta(hours=6)):
        for i in range(8):
            rows.append({"t": t, "week": "2026-W02", "ticker": f"T{i}", "event_ticker": "E", "kind": "monthly",
                         "p_nbm": 0.9, "bid": 0.30, "ask": 0.35, "volume": 10_000.0, "y": 1})  # fmt: skip
    trades = simulate_taker(pd.DataFrame(rows), 0.10)
    assert trades["ticker"].is_unique  # one position per market per day
    assert trades["contracts"].sum() == 1000  # event-day cap
    assert trades["contracts"].max() == 250  # market-day cap
    assert (trades["side"] == "yes").all() and (trades["pnl"] > 0).all()


def test_synthetic_nbm_is_truth_market_is_noisy_gives_go():
    f, q, y, ends = synthetic_world(nbm_noise=0.0, mkt_noise=1.2, seed=3)
    r = score_all(f, q, y, ends, n_boot=2000)
    assert r["primary"]["ci_low"] > 0
    assert r["verdict"] == "GO"


def test_synthetic_market_is_truth_nbm_is_noisy_gives_no_go():
    f, q, y, ends = synthetic_world(nbm_noise=1.2, mkt_noise=0.0, seed=4)
    r = score_all(f, q, y, ends, n_boot=2000)
    assert r["primary"]["ci_high"] < 0
    assert r["verdict"] == "NO GO"


def test_synthetic_equal_forecasters_do_not_give_go():
    f, q, y, ends = synthetic_world(nbm_noise=0.5, mkt_noise=0.5, seed=5)
    r = score_all(f, q, y, ends, n_boot=2000)
    assert r["verdict"] != "GO"
