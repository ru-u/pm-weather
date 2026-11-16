"""Phase 4 ERCOT peak model: no-lookahead dataset, observed-peak combination,
strike grid, and calibration of the probabilistic models on synthetic data."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from pmw.models import ercot_peak as ep


def _tables(days: list[date]) -> ep.Tables:
    """A small world: a daily load curve peaking at HE 17, forecasts posted at the kept slots."""
    hourly, fch, fpk, telh, tel5, daily = [], [], [], [], [], []
    doc = 0
    for i, d in enumerate(days):
        level = 50000 + 1000 * i
        curve = {he: level * (0.8 + 0.2 * math.exp(-((he - 17) ** 2) / 18)) for he in range(1, 25)}
        for he, v in curve.items():
            hourly.append({"oper_day": d, "hour_ending": he, "repeated": False, "total": v})
            telh.append(
                {
                    "oper_day": d,
                    "hour_ending": he,
                    "repeated": False,
                    "mean_load": v * 1.002,
                    "n": 12,
                }
            )
            for m5 in range(12):
                ts = datetime.combine(d, datetime.min.time(), tzinfo=ep.CT) + timedelta(
                    hours=he - 1, minutes=5 * m5
                )
                tel5.append({"ts_utc": ts.astimezone(ep.UTC), "oper_day": d, "system": v})
        daily.append({"oper_day": d, "peak_first_complete": max(curve.values())})
        for slot in (23, 5, 9, 13, 16):
            pd_ = d - timedelta(days=1) if slot == 23 else d
            post = datetime.combine(pd_, datetime.min.time(), tzinfo=ep.CT) + timedelta(
                hours=slot, minutes=30
            )
            doc += 1
            bias = 1.01 if slot in (23, 5) else 1.0
            for he, v in curve.items():
                fch.append({"doc_id": doc, "post_utc": post.astimezone(ep.UTC), "delivery_day": d,
                            "hour_ending": he, "repeated": False, "system_total": v * bias})  # fmt: skip
            fpk.append({"doc_id": doc, "post_utc": post.astimezone(ep.UTC), "delivery_day": d,
                        "model": "INUSE", "in_use": True, "peak": max(curve.values()) * bias})  # fmt: skip
            for mname, f in (("A", 0.99), ("B", 1.02)):
                fpk.append({"doc_id": doc, "post_utc": post.astimezone(ep.UTC), "delivery_day": d,
                            "model": mname, "in_use": False, "peak": max(curve.values()) * bias * f})  # fmt: skip
    t = ep.Tables(
        pd.DataFrame(daily), pd.DataFrame(hourly), pd.DataFrame(fch), pd.DataFrame(fpk),
        pd.DataFrame(telh), pd.DataFrame(tel5),
    )  # fmt: skip
    for df in (t.fc_hourly, t.fc_peaks, t.tel_5min):
        col = "post_utc" if "post_utc" in df else "ts_utc"
        df[col] = pd.to_datetime(df[col], utc=True)
    return t


def test_dataset_uses_only_information_before_the_decision_time():
    days = [date(2024, 7, 1) + timedelta(days=i) for i in range(20)]
    t = _tables(days)
    ds = ep.build_dataset(t, days[0], days[-1])
    assert len(ds) == 20 * len(ep.DECISION_HOURS)
    assert (ds["post_utc"] < ds["t"]).all()
    r = ds[(ds["oper_day"] == days[10]) & (ds["hour"] == 14)].iloc[0]
    # H uses completed hours HE 1..14 only (curve still rising toward HE 17).
    level = 50000 + 1000 * 10
    curve14 = level * (0.8 + 0.2 * math.exp(-((14 - 17) ** 2) / 18))
    assert r["H_tel"] == pytest.approx(curve14 * 1.002)
    assert r["A"] == pytest.approx(level)  # the peak is at HE 17, after 14:00
    assert r["R"] == pytest.approx(level)
    # 00:00 decision: nothing observed, the 23:30 posting (slot 23) with its 1% high bias.
    r0 = ds[(ds["oper_day"] == days[10]) & (ds["hour"] == 0)].iloc[0]
    assert math.isnan(r0["H_tel"]) and r0["F"] == pytest.approx(level * 1.01)
    # Recent bias from earlier days only: telemetry max / day-ahead forecast = 1.002 / 1.01.
    assert r0["bias_3"] == pytest.approx(1.002 / 1.01)
    first = ds[ds["oper_day"] == days[0]].iloc[0]
    assert math.isnan(first["bias_3"])  # no earlier days in the world


def test_observed_peak_above_strike_makes_yes_certain():
    rows = pd.DataFrame({"H_tel": [60000.0, math.nan], "F": [55000.0, 55000.0]})
    strikes = np.array([[55000.0, 61000.0], [55000.0, 61000.0]])
    p_R = np.array([[0.3, 0.1], [0.3, 0.1]])
    gap = np.array([0.0, 0.001, -0.001])
    p = ep.combine_with_observed(rows, p_R, strikes, gap)
    assert p[0, 0] == pytest.approx(1.0)  # 60,000 already observed > 55,000
    assert p[0, 1] == pytest.approx(0.1)  # 61,000 not yet reached: only R can do it
    assert np.allclose(p[1], p_R[1])  # nothing observed at 00:00


def test_strike_grid_is_kalshi_like():
    g = ep.strike_grid(pd.DataFrame({"F": [72340.0]}))
    assert g.shape == (1, 17) and g[0, 8] == 72500.0 and np.allclose(np.diff(g[0]), 500.0)


def _synthetic_rows(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    X = pd.DataFrame({f: rng.normal(size=n) for f in ep.FEATURES})
    X["y"] = 0.01 * X["F_level"] + rng.normal(0, 0.02 + 0.01 * (X["hour"] > 0), n)
    return X


@pytest.mark.parametrize("model", [ep.LGBMConformal, ep.NGBoostNormal])
def test_probabilistic_models_are_calibrated_out_of_sample(model):
    train, calib, test = (
        _synthetic_rows(3000, 1),
        _synthetic_rows(1000, 2),
        _synthetic_rows(2000, 3),
    )
    m = model().fit(train, calib)
    test = test.assign(F_rem=50000.0)
    strikes = 50000.0 * np.exp(np.quantile(test["y"], [0.1, 0.5, 0.9]))[None, :].repeat(
        len(test), 0
    )
    p = m.prob_R_above(test, strikes)
    hit = (50000.0 * np.exp(test["y"].to_numpy())[:, None] > strikes).astype(float)
    # Mean forecast probability matches the observed frequency at each strike.
    assert np.allclose(p.mean(axis=0), hit.mean(axis=0), atol=0.05)
    if isinstance(m, ep.LGBMConformal):
        cov = ep.interval_coverage(m, test)
        assert abs(cov["80%"] - 0.80) < 0.05 and abs(cov["95%"] - 0.95) < 0.03
