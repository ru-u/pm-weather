"""The public CLI works on its own: no private plugins, no params file."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from pmw import plugins as pl
from pmw.cli import app

FIX = Path(__file__).parent / "fixtures"
runner = CliRunner()


@pytest.fixture(autouse=True)
def public_only(monkeypatch):
    monkeypatch.delenv(pl.PARAMS_ENV, raising=False)
    monkeypatch.setattr(pl, "_discovered", lambda group: {})


def _obs_file(tmp_path: Path, p_comp: float, y: int) -> Path:
    book = json.loads((FIX / "logged_book_KXSNOWCITIES-26NOV15-NYC.json").read_text())
    mid = (book["best_yes_ask"] + (book["best_yes_bid"] or 0.0)) / 2
    row = {k: book[k] for k in ("ticker", "event_ticker", "yes_px", "yes_qty", "no_px", "no_qty")}
    row.update(t=pd.Timestamp(book["received_at_utc"]), mid=mid, p_comp=p_comp, y=y)
    path = tmp_path / "obs.parquet"
    pd.DataFrame([row]).to_parquet(path)
    return path


def test_help_runs_as_a_module():
    r = subprocess.run([sys.executable, "-m", "pmw.cli", "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "backtest" in r.stdout and "settle-check" in r.stdout


def test_no_private_commands_listed():
    out = runner.invoke(app, ["--help"]).stdout
    for name in ("p4s", "p4e", "snow-model", "snow-freeze"):
        assert name not in out


def test_plugins_reports_built_in_defaults():
    r = runner.invoke(app, ["plugins"])
    assert r.exit_code == 0, r.output
    info = json.loads(r.stdout)
    assert info["params"] == "built-in defaults"
    assert info["strategies"] == {"naive": "built-in"} and info["models"] == {"column": "built-in"}


def test_backtest_on_a_real_logged_book(tmp_path):
    # Real book: best NO bid 0.99, so YES is offered at 0.01.
    r = runner.invoke(app, ["backtest", str(_obs_file(tmp_path, p_comp=0.30, y=0)),
                            "--out", str(tmp_path / "trades.parquet")])  # fmt: skip
    assert r.exit_code == 0, r.output
    s = json.loads(r.stdout)
    assert (s["strategy"], s["model"], s["entries"]) == ("naive", "column", 1)
    assert s["contracts"] == 250.0 and s["pnl"] < 0  # capped at 250, YES lost
    assert len(pd.read_parquet(tmp_path / "trades.parquet")) == 1


def test_backtest_no_edge_no_entry(tmp_path):
    r = runner.invoke(app, ["backtest", str(_obs_file(tmp_path, p_comp=0.0, y=0))])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["entries"] == 0


def test_unknown_strategy_fails_cleanly(tmp_path):
    r = runner.invoke(
        app, ["backtest", str(_obs_file(tmp_path, 0.3, 0)), "--strategy", "storm_taker"]
    )
    assert r.exit_code != 0 and "unknown strategy" in str(r.exception)
