"""P3A scoring: slot and leakage logic, the probability, the decision rule, and the
registered synthetic known-answer runs (GO, NO GO, CONDITIONAL as expected)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from pmw.eval import p2a_score as p2
from pmw.eval import p3a_score as p3


def test_slots_relative_to_delivery_day():
    d = date(2026, 9, 1)
    # 23:30 CDT Aug 31 = 04:30 UTC Sep 1: the evening-before slot.
    assert p3.slot_of(datetime(2026, 9, 1, 4, 30, tzinfo=UTC), d) == 23
    assert p3.slot_of(datetime(2026, 9, 1, 10, 30, tzinfo=UTC), d) == 5  # 05:30 CDT
    assert p3.slot_of(datetime(2026, 9, 1, 21, 30, tzinfo=UTC), d) == 16  # 16:30 CDT
    assert p3.slot_of(datetime(2026, 9, 1, 15, 30, tzinfo=UTC), d) is None  # 10:30, not kept
    assert p3.slot_of(datetime(2026, 8, 31, 10, 30, tzinfo=UTC), d) is None  # day-ahead morning


def test_decision_uses_only_postings_strictly_before_it():
    d = date(2026, 9, 1)
    posts = pd.DataFrame(
        {
            "delivery_day": [d, d, d],
            "post_utc": pd.to_datetime(
                ["2026-09-01T04:30Z", "2026-09-01T10:30Z", "2026-09-01T11:00Z"], utc=True
            ),
            "slot": [23, 5, None],
            "model": ["X"] * 3,
            "F": [70000.0, 71000.0, 99999.0],
        }
    )
    t06 = p3.decision_times(d, (6,))[0]  # 06:00 CDT = 11:00 UTC
    f = p3.forecast_at(posts, d, t06)
    assert f is not None and f["F"] == 71000.0  # the 11:00 UTC posting is not before 11:00
    t00 = p3.decision_times(d, (0,))[0]
    assert p3.forecast_at(posts, d, t00)["slot"] == 23


def test_probability_is_the_share_of_training_ratios_above_the_strike():
    r = np.array([0.97, 0.99, 1.0, 1.01, 1.03])
    assert p3.prob_above(70000, 70000, r) == pytest.approx(0.4)  # strictly above
    assert p3.prob_above(70000, 60000, r) == 1.0


def test_doy_window_is_circular():
    doys = np.array([1, 20, 340, 365, 200])
    assert list(p3._doy_close(doys, 5)) == [True, True, True, True, False]


def test_capacity_clause_blocks_go_only():
    good = p2.DiffResult(100, 12, 0.02, 0.01, 0.03)
    bad = p2.DiffResult(100, 12, -0.02, -0.03, -0.01)
    assert p3.decide(good, 0.95, [], capacity_ok=True) == "GO"
    assert p3.decide(good, 0.95, [], capacity_ok=False) == "CONDITIONAL"
    assert p3.decide(bad, 0.2, [], capacity_ok=False) == "NO GO"
    flip = p2.DiffResult(100, 12, -0.001, -0.01, 0.01)
    assert p3.decide(good, 0.95, [flip], capacity_ok=True) == "CONDITIONAL"


@pytest.mark.parametrize(
    ("ercot_noise", "market_noise", "volume", "expected"),
    [
        (0.005, 0.06, 5000.0, "GO"),  # ERCOT much sharper, market deep enough
        (0.005, 0.06, 1000.0, "CONDITIONAL"),  # same edge, capacity gate fails
        (0.06, 0.005, 5000.0, "NO GO"),  # market much sharper
    ],
)
def test_synthetic_known_answers(ercot_noise, market_noise, volume, expected):
    markets, quotes, postings, actual = p3.synthetic_world(ercot_noise, market_noise, volume=volume)
    r = p3.score(markets, quotes, postings, actual, n_boot=500)
    assert r["leakage"]["posting_not_before_decision"] == 0
    assert r["verdict"] == expected, (r["primary"], r["taker"]["0.1"]["share_resamples_positive"])
