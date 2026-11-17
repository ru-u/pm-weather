from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from pmw import plugins as pl
from pmw.backtest.taker import Exposure, Observation, run, settle, walk
from pmw.strategy import NaiveTaker


def _obs(p: float, **kw: object) -> Observation:
    base = dict(ticker="M-1", event_ticker="E", t=datetime(2026, 11, 20, tzinfo=UTC), p_yes=p,
                mid=0.30, yes_px=[0.20, 0.28], yes_qty=[50.0, 10.0],
                no_px=[0.60, 0.68], no_qty=[100.0, 20.0])  # fmt: skip
    base.update(kw)
    return Observation(**base)  # type: ignore[arg-type]


def test_walk_takes_levels_while_after_fee_edge_exceeds_tau():
    # NO bids 0.68 (best, last) and 0.60 offer YES at 0.32 then 0.40.
    fills = walk(0.55, [0.60, 0.68], [100.0, 20.0], tau=0.10, cap=1000)
    assert [(f.price, f.contracts) for f in fills] == [(0.32, 20.0), (0.40, 100.0)]
    assert walk(0.45, [0.60, 0.68], [100.0, 20.0], tau=0.10, cap=1000)[-1].price == 0.32
    assert walk(0.40, [0.60, 0.68], [100.0, 20.0], tau=0.10, cap=1000) == []


def test_naive_taker_defaults_are_the_registered_baseline():
    s = NaiveTaker()
    assert (s.tau, s.cap_market, s.cap_event) == (0.10, 250.0, 1000.0)


def test_naive_taker_one_entry_per_market_and_event_cap():
    s = NaiveTaker(cap_market=15, cap_event=25)
    ex = Exposure()
    e = s.decide(_obs(0.60), ex)
    assert e is not None and e.side == "yes" and e.contracts == 15
    ex.entered.add("M-1")
    ex.event_contracts["E"] = 15
    assert s.decide(_obs(0.60), ex) is None
    e2 = s.decide(_obs(0.60, ticker="M-2"), ex)
    assert e2 is not None and e2.contracts == 10  # 25 per event minus 15 used


def test_naive_taker_buys_no_when_model_is_below_market():
    e = NaiveTaker().decide(_obs(0.05), Exposure())
    assert e is not None and e.side == "no" and e.fills[0].price == 0.72  # 1 - best YES bid 0.28


def test_run_and_settle():
    df = pd.DataFrame([
        {**_obs(0.60).__dict__, "p_model": 0.60, "y": 1},
        {**_obs(0.60, ticker="M-2").__dict__, "p_model": 0.30, "y": 0},
    ])  # fmt: skip
    trades = run(df, NaiveTaker())
    assert list(trades["ticker"]) == ["M-1"]
    out = settle(trades, {"M-1": 1})
    row = out.iloc[0]
    assert row["pnl"] == pytest.approx(row["contracts"] - row["cost"] - row["fees"])


def test_registry_without_plugins_has_baselines(monkeypatch):
    monkeypatch.setattr(pl, "_discovered", lambda group: {})
    assert pl.available() == {"strategies": {"naive": "built-in"}, "models": {"column": "built-in"}}
    assert isinstance(pl.get_strategy(), NaiveTaker)
    assert isinstance(pl.get_model(), pl.ColumnModel)
    with pytest.raises(KeyError, match="unknown strategy"):
        pl.get_strategy("not_installed")


def test_params_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv(pl.PARAMS_ENV, raising=False)
    assert pl.load_params() == {}
    f = tmp_path / "p.yaml"
    f.write_text("strategy: naive\nnaive: {tau: 0.2}\n")
    monkeypatch.setenv(pl.PARAMS_ENV, str(f))
    assert pl.get_strategy(params=pl.load_params()).tau == 0.2  # type: ignore[attr-defined]
    g = tmp_path / "q.yaml"
    g.write_text("naive: {tau: 0.3}\n")
    assert pl.load_params(g)["naive"]["tau"] == 0.3  # explicit path wins over the env var
    monkeypatch.setenv(pl.PARAMS_ENV, str(tmp_path / "missing.yaml"))
    with pytest.raises(FileNotFoundError):
        pl.load_params()


def test_example_params_file_matches_defaults():
    from pathlib import Path

    cfg = pl.load_params(Path(__file__).parents[1] / "configs" / "params.example.yaml")
    s = pl.get_strategy(params=cfg)
    assert (s.tau, s.cap_market, s.cap_event) == (0.10, 250.0, 1000.0)  # type: ignore[attr-defined]
