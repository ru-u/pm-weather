"""P2B scoring on synthetic data with a known answer (run before the test winter)."""

import numpy as np
import pandas as pd

from pmw.eval import p2b


def test_overconfident_forecasts_are_flagged_and_slope_is_below_one():
    dev, test = p2b.synthetic(overconfident=True)
    r = p2b.score(dev, test, n_boot=300)
    _, b = r["coefficients"]["K1_n1"]
    assert b < 0.7  # the 2.2x stretch of log-odds is undone
    assert r["primary"]["weakness_documented"] is True


def test_calibrated_forecasts_are_not_flagged():
    dev, test = p2b.synthetic(overconfident=False)
    r = p2b.score(dev, test, n_boot=300)
    a, b = r["coefficients"]["K1_n1"]
    assert 0.8 < b < 1.25 and abs(a) < 0.3
    assert r["primary"]["weakness_documented"] is False


def test_recalibration_recovers_a_known_map():
    rng = np.random.default_rng(1)
    p = rng.uniform(0.01, 0.99, 20000)
    true = p2b.apply_recalibration(p, (-0.5, 0.6))
    y = (rng.random(len(p)) < true).astype(float)
    a, b = p2b.fit_recalibration(p, y)
    assert abs(a + 0.5) < 0.08 and abs(b - 0.6) < 0.05


def test_long_form_outcomes_are_strictly_above_threshold():
    cases = pd.DataFrame(
        {
            "station": ["KNYC"] * 3, "season": [2026] * 3,
            "start": pd.to_datetime(["2026-01-05"] * 3).date, "n": [1] * 3, "lead": [1] * 3,
            "versions": ["V4.3"] * 3, "total": [0.1, 1.0, 1.1],
            **{f"p_{k:g}": [0.5] * 3 for k in p2b.THRESHOLDS},
            **{f"c_{k:g}": [0.1] * 3 for k in p2b.THRESHOLDS},
        }
    )  # fmt: skip
    lf = p2b.long_form(cases)
    y01 = lf[lf["k"] == 0.1]["y"].tolist()
    y1 = lf[lf["k"] == 1.0]["y"].tolist()
    assert y01 == [0.0, 1.0, 1.0]  # 0.1 in is not "above 0.1"
    assert y1 == [0.0, 0.0, 1.0]
    assert set(lf["week"]) == {"2026-W02"}
