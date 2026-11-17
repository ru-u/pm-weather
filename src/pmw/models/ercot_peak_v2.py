"""Phase 4, ERCOT nowcast v2: weather features, more model families, a log-odds blend.

Same target, rows, splits and strike grid as v1 (`ercot_peak`). Adds:
  - weather features (`pmw.features.ercot_weather`): NBM hourly temperature
    forecasts for the remaining hours and ASOS temperatures observed so far;
  - NGBoost with Laplace and Student-t (df 3) distributions on y;
  - XGBoost multi-quantile regression with the same conformal step as LightGBM;
  - a blend in log-odds of P(peak > K): logit p = b + sum_m w_m logit p_m.

Blend weights are fitted on validation. To report an honest validation score
for the blend, weights are cross-fitted: each calendar quarter of validation
is scored with weights fitted on the other quarters. The final weights (used
later, out of sample) are fitted on all of validation.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats
from scipy.optimize import minimize

from pmw.features.ercot_weather import WX_FEATURES
from pmw.models import ercot_peak as ep

FEATURES_V2 = [*ep.FEATURES, *WX_FEATURES]
CLIP = 1e-4


# ---------------------------------------------------------------------------
# Weather features joined onto the v1 rows
# ---------------------------------------------------------------------------


def build_weather(
    ds: pd.DataFrame, hourly: pd.DataFrame, nbh_root: Path, asos_root: Path
) -> pd.DataFrame:
    from pmw.features import ercot_weather as ew
    from pmw.utils.config import load_ercot_stations_config

    cfg = load_ercot_stations_config().stations
    zone_of: dict[str, str] = {k: str(v.zone) for k, v in cfg.items()}
    days = sorted(set(ds["oper_day"]))
    nbh = ew.load_nbh(nbh_root, days, sorted(cfg))
    asos = ew.load_asos(asos_root, {v.asos: k for k, v in cfg.items()})
    w = ew.zone_weights(hourly)
    wx = ew.weather_features(ds, nbh, asos, w, zone_of)
    return pd.concat([ds[["oper_day", "hour"]], wx], axis=1)


# ---------------------------------------------------------------------------
# More model families
# ---------------------------------------------------------------------------


class NGBoostDist:
    """NGBoost on y with a chosen distribution and a scale multiplier fitted on the
    calibration year by negative log likelihood."""

    def __init__(self, dist: str, features: list[str], name: str | None = None) -> None:
        self.dist, self.features = dist, list(features)
        self.name = name or f"ngboost_{dist}"

    def fit(self, train: pd.DataFrame, calib: pd.DataFrame) -> NGBoostDist:
        from ngboost import NGBRegressor  # type: ignore[import-untyped]
        from ngboost.distns import Laplace, Normal, TFixedDf  # type: ignore[import-untyped]

        D = {"normal": Normal, "laplace": Laplace, "t": TFixedDf}[self.dist]
        self.m = NGBRegressor(
            Dist=D, n_estimators=600, learning_rate=0.03, verbose=False, random_state=7
        )
        self.m.fit(train[self.features].to_numpy(float), train["y"].to_numpy(float))
        loc, sc = self._raw(calib)
        yc = calib["y"].to_numpy(float)
        grid = np.linspace(0.6, 2.0, 57)
        nll = [-self._frozen(loc, sc * g).logpdf(yc).mean() for g in grid]
        self.scale = float(grid[int(np.argmin(nll))])
        return self

    def _frozen(self, loc: np.ndarray, sc: np.ndarray) -> Any:
        if self.dist == "normal":
            return stats.norm(loc, sc)
        if self.dist == "laplace":
            return stats.laplace(loc, sc)
        return stats.t(3, loc, sc)

    def _raw(self, rows: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        d = self.m.pred_dist(rows[self.features].to_numpy(float))
        return np.asarray(d.params["loc"]), np.asarray(d.params["scale"])

    def cdf_y(self, rows: pd.DataFrame, y: np.ndarray) -> np.ndarray:
        loc, sc = self._raw(rows)
        return np.asarray(self._frozen(loc[:, None], sc[:, None] * self.scale).cdf(y))

    def prob_R_above(self, rows: pd.DataFrame, strikes: np.ndarray) -> np.ndarray:
        return 1 - self.cdf_y(rows, np.log(strikes / rows["F_rem"].to_numpy()[:, None]))


class XGBQuantileConformal(ep.LGBMConformal):
    """XGBoost multi-quantile model, conformalized on the calibration year like LightGBM."""

    name = "xgb_cqr"

    def fit(self, train: pd.DataFrame, calib: pd.DataFrame) -> XGBQuantileConformal:
        import xgboost as xgb

        self.m = xgb.XGBRegressor(
            objective="reg:quantileerror", quantile_alpha=np.array(ep.QUANTILES),
            n_estimators=500, learning_rate=0.03, max_depth=4, min_child_weight=20,
            subsample=0.8, colsample_bytree=0.8, random_state=7, tree_method="hist",
        )  # fmt: skip
        self.m.fit(train[self.features].to_numpy(float), train["y"].to_numpy(float))
        qc = self._raw(calib)
        yc = calib["y"].to_numpy(float)
        self.shift = {q: float(np.quantile(yc - qc[:, j], q)) for j, q in enumerate(ep.QUANTILES)}
        return self

    def _raw(self, rows: pd.DataFrame) -> np.ndarray:
        return np.asarray(self.m.predict(rows[self.features].to_numpy(float)))


# ---------------------------------------------------------------------------
# Probabilities, blend, scores
# ---------------------------------------------------------------------------


def prob_above(m: Any, rows: pd.DataFrame, strikes: np.ndarray, gap: np.ndarray) -> np.ndarray:
    if hasattr(m, "prob_R_above"):
        return ep.combine_with_observed(rows, m.prob_R_above(rows, strikes), strikes, gap)
    return np.asarray(m.prob_above(rows, strikes))


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, CLIP, 1 - CLIP)
    return np.log(p / (1 - p))


def fit_blend(P: list[np.ndarray], y: np.ndarray) -> np.ndarray:
    """Weights [b, w_1..w_M] minimizing Brier of sigmoid(b + sum w_m logit p_m)."""
    X = np.stack([_logit(p).ravel() for p in P], axis=1)
    yy = y.ravel()

    def loss(w: np.ndarray) -> float:
        q = 1 / (1 + np.exp(-(w[0] + X @ w[1:])))
        return float(np.mean((q - yy) ** 2))

    w0 = np.r_[0.0, np.full(len(P), 1.0 / len(P))]
    return np.asarray(minimize(loss, w0, method="L-BFGS-B").x)


def apply_blend(w: np.ndarray, P: list[np.ndarray]) -> np.ndarray:
    z = w[0] + sum(wi * _logit(p) for wi, p in zip(w[1:], P, strict=True))
    return np.asarray(1 / (1 + np.exp(-z)))


def crossfit_blend(
    P: list[np.ndarray], y: np.ndarray, rows: pd.DataFrame
) -> tuple[np.ndarray, np.ndarray]:
    """Out-of-quarter blended probabilities, and the weights fitted on all rows."""
    q = pd.PeriodIndex(pd.to_datetime(rows["oper_day"]), freq="Q").astype(str).to_numpy()
    out = np.empty_like(P[0])
    for k in np.unique(q):
        m = q == k
        w = fit_blend([p[~m] for p in P], y[~m])
        out[m] = apply_blend(w, [p[m] for p in P])
    return out, fit_blend(P, y)


def score(p: np.ndarray, rows: pd.DataFrame, strikes: np.ndarray) -> dict[str, Any]:
    b = ep.brier_grid(p, rows, strikes)
    res: dict[str, Any] = {"brier": round(float(b.mean()), 5), "by_hour": {}}
    for h in ep.DECISION_HOURS:
        mask = (rows["hour"] == h).to_numpy()
        if mask.any():
            res["by_hour"][h] = round(float(b[mask].mean()), 5)
    hot = (rows["A"] >= rows["A"].quantile(0.8)).to_numpy()
    res["hot_days"] = round(float(b[hot].mean()), 5)
    return res


def pit_deciles(cdf_at_y: np.ndarray) -> list[float]:
    """Share of PIT values in each tenth (0.1 each when calibrated)."""
    h, _ = np.histogram(np.clip(cdf_at_y, 0, 1), bins=np.linspace(0, 1, 11))
    return [round(float(x), 3) for x in h / max(len(cdf_at_y), 1)]


def quantile_pit(m: ep.LGBMConformal, rows: pd.DataFrame) -> np.ndarray:
    qs = m.quantiles(rows)
    y = rows["y"].to_numpy(float)
    lv = np.array(ep.QUANTILES)
    return np.array([np.interp(y[i], qs[i], lv, left=0.0, right=1.0) for i in range(len(rows))])


def lgbm_importance(m: ep.LGBMConformal) -> dict[str, float]:
    g = m.models[0.5].booster_.feature_importance("gain")
    share = g / g.sum()
    order = np.argsort(-share)
    return {m.features[i]: round(float(share[i]), 4) for i in order}


def run(
    ds: pd.DataFrame, wx: pd.DataFrame, t: ep.Tables, log: Callable[[str], None] = print
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fit everything on train (+ calib), score on validation. Returns (report, fitted)."""
    data = ds.merge(wx, on=["oper_day", "hour"], how="left")
    parts = ep.split(data)
    tr, ca, va = parts["train"], parts["calib"], parts["valid"]
    gap = ep.telemetry_gap(t, ep.TRAIN_END)
    strikes = ep.strike_grid(va)
    y_va = (va["A"].to_numpy()[:, None] > strikes).astype(float)

    specs: dict[str, Any] = {
        "baseline_p3a": ep.Baseline(),
        "ngboost_v1": ep.NGBoostNormal(ep.FEATURES),
        "lgbm_v1": ep.LGBMConformal(ep.FEATURES),
        "ngboost_normal_v2": NGBoostDist("normal", FEATURES_V2, "ngboost_normal_v2"),
        "ngboost_laplace_v2": NGBoostDist("laplace", FEATURES_V2, "ngboost_laplace_v2"),
        "ngboost_t_v2": NGBoostDist("t", FEATURES_V2, "ngboost_t_v2"),
        "lgbm_v2": ep.LGBMConformal(FEATURES_V2),
        "xgb_v2": XGBQuantileConformal(FEATURES_V2),
    }
    fitted: dict[str, Any] = {}
    probs: dict[str, np.ndarray] = {}
    report: dict[str, Any] = {"models": {}}
    for name, m in specs.items():
        log(f"fitting {name}")
        fitted[name] = m.fit(tr, ca)
        probs[name] = prob_above(fitted[name], va, strikes, gap)
        report["models"][name] = score(probs[name], va, strikes)

    # Blends (cross-fitted score, weights on all of validation).
    blends = {
        "blend_v1": ["baseline_p3a", "ngboost_v1", "lgbm_v1"],
        "blend_v2_core": ["baseline_p3a", "ngboost_normal_v2", "lgbm_v2"],
        "blend_v2_all": [
            "baseline_p3a", "ngboost_normal_v2", "ngboost_laplace_v2", "ngboost_t_v2",
            "lgbm_v2", "xgb_v2",
        ],
    }  # fmt: skip
    report["blend_weights"] = {}
    for bname, members in blends.items():
        p_cf, w = crossfit_blend([probs[m] for m in members], y_va, va)
        probs[bname] = p_cf
        report["models"][bname] = score(p_cf, va, strikes)
        report["blend_weights"][bname] = dict(
            zip(["intercept", *members], [round(float(x), 4) for x in w], strict=True)
        )

    # Calibration of the distribution of y (remaining-hours peak / forecast).
    report["pit_deciles_valid"] = {
        "lgbm_v1": pit_deciles(quantile_pit(fitted["lgbm_v1"], va)),
        "lgbm_v2": pit_deciles(quantile_pit(fitted["lgbm_v2"], va)),
        "xgb_v2": pit_deciles(quantile_pit(fitted["xgb_v2"], va)),
        "ngboost_normal_v2": pit_deciles(
            fitted["ngboost_normal_v2"].cdf_y(va, va["y"].to_numpy()[:, None])[:, 0]
        ),
    }
    report["interval_coverage_valid"] = {
        k: ep.interval_coverage(fitted[k], va) for k in ("lgbm_v1", "lgbm_v2", "xgb_v2")
    }
    report["lgbm_v2_importance"] = lgbm_importance(fitted["lgbm_v2"])
    cover = data[WX_FEATURES].notna().groupby(pd.to_datetime(data["oper_day"]).dt.year).mean()
    report["wx_feature_coverage_by_year"] = cover.round(3).to_dict(orient="index")
    report["rows"] = {k: len(v) for k, v in parts.items()}
    return report, fitted
