"""Phase 4, ERCOT: nowcasting the daily peak (docs/phase4_ercot_plan.md).

Target decomposition, so the intraday structure is exact, not learned:
  H = highest hourly load already completed at decision time t (real-time telemetry)
  R = highest settled hourly load in the hours still to come
  A = max(H_settled, R)              (A is the first-complete NP6-346-CD peak)
The models predict y = log(R / F_rem), with F_rem the in-use ERCOT forecast's
maximum over the remaining hours. P(A > K) = 1 - P(H_settled <= K) * P(R <= K).

Every feature for (day D, time t) uses only data public before t:
  - forecast postings with postDatetime < t (latest kept slot, as in P3A);
  - telemetry (NP3-910-ER, standing in for ERCOT's live dashboard) up to t;
  - "recent bias" from telemetry daily maxima of days before D (known live).
Settled hourly loads (NP6-346-CD) are used only as targets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.stats import norm

from pmw.energy.ercot import MIN_POINTS_PER_HOUR

CT = ZoneInfo("America/Chicago")
DECISION_HOURS = (0, 6, 10, 14, 17)
SLOT_FOR_HOUR = {0: 23, 6: 5, 10: 9, 14: 13, 17: 16}
TRAIN_END = date(2023, 12, 31)  # fit
CALIB_END = date(2024, 12, 31)  # calibrate (sigma scale, conformal)
VALID_END = date(2026, 8, 27)  # choose models and blends; last day before Kalshi
QUANTILES = (0.01, 0.025, *[round(0.05 * i, 2) for i in range(1, 20)], 0.975, 0.99)
STRIKE_STEP = 500.0
STRIKE_SPAN = 4000.0


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


def decision_time(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=CT).astimezone(UTC)


@dataclass
class Tables:
    daily: pd.DataFrame  # oper_day, peak_first_complete
    hourly: pd.DataFrame  # oper_day, hour_ending, repeated, total (settled)
    fc_hourly: pd.DataFrame  # doc_id, post_utc, delivery_day, hour_ending, repeated, system_total
    fc_peaks: pd.DataFrame  # doc_id, post_utc, delivery_day, model, in_use, peak
    tel_hourly: pd.DataFrame  # oper_day, hour_ending, repeated, mean_load, n
    tel_5min: pd.DataFrame  # ts_utc, oper_day, system

    @classmethod
    def load(cls, root: Path) -> Tables:
        r = lambda n: pd.read_parquet(root / n)  # noqa: E731
        t = cls(
            r("daily_peak.parquet"), r("hourly_load.parquet"), r("forecast_hourly.parquet"),
            r("forecast_peaks.parquet"), r("telemetry_hourly.parquet"),
            r("telemetry_5min.parquet")[["ts_utc", "oper_day", "system"]],
        )  # fmt: skip
        for df in (t.fc_hourly, t.fc_peaks):
            df["post_utc"] = pd.to_datetime(df["post_utc"], utc=True)
        t.tel_5min["ts_utc"] = pd.to_datetime(t.tel_5min["ts_utc"], utc=True)
        for df in (t.daily, t.hourly, t.fc_hourly, t.fc_peaks, t.tel_hourly, t.tel_5min):
            col = "oper_day" if "oper_day" in df else "delivery_day"
            df[col] = pd.to_datetime(df[col]).dt.date
        return t


def _holidays(start: date, end: date) -> set[date]:
    from pandas.tseries.holiday import USFederalHolidayCalendar

    return {d.date() for d in USFederalHolidayCalendar().holidays(start, end)}


def build_dataset(t: Tables, start: date, end: date) -> pd.DataFrame:
    """One row per (operating day, decision hour) with features and target."""
    settled = {
        d: g.sort_values(["hour_ending", "repeated"]) for d, g in t.hourly.groupby("oper_day")
    }
    tel_h = {d: g for d, g in t.tel_hourly.groupby("oper_day")}
    tel_max = (
        t.tel_hourly[t.tel_hourly["n"] >= MIN_POINTS_PER_HOUR]
        .groupby("oper_day")["mean_load"]
        .max()
    )
    inuse = t.fc_peaks[t.fc_peaks["in_use"]]
    models = t.fc_peaks[~t.fc_peaks["in_use"]]
    inuse_by_day = {d: g.sort_values("post_utc") for d, g in inuse.groupby("delivery_day")}
    models_by_doc = {(doc, d): g for (doc, d), g in models.groupby(["doc_id", "delivery_day"])}
    fch = {(doc, d): g for (doc, d), g in t.fc_hourly.groupby(["doc_id", "delivery_day"])}
    tel5 = t.tel_5min.set_index("ts_utc").sort_index()["system"]
    # Day-ahead (23:30 the evening before) in-use forecast per day, for recent-bias features.
    da = {}
    for d, g in inuse_by_day.items():
        cut = decision_time(d, 0)
        p = g[g["post_utc"] < cut]
        if len(p):
            da[d] = float(p.iloc[-1]["peak"])
    holidays = _holidays(start - timedelta(days=30), end + timedelta(days=30))

    rows = []
    d = start
    while d <= end:
        if d not in settled or d not in inuse_by_day:
            d += timedelta(days=1)
            continue
        hours = settled[d]
        for h in DECISION_HOURS:
            tdec = decision_time(d, h)
            posts = inuse_by_day[d][inuse_by_day[d]["post_utc"] < tdec]
            if posts.empty:
                continue
            post = posts.iloc[-1]
            post_local = post["post_utc"].astimezone(CT)
            slot_ok = (
                h == 0 and post_local.date() == d - timedelta(days=1) and post_local.hour == 23
            ) or (h > 0 and post_local.date() == d and post_local.hour == SLOT_FOR_HOUR[h])
            if not slot_ok:
                continue
            fc = fch.get((post["doc_id"], d))
            if fc is None:
                continue
            rem_fc = fc[fc["hour_ending"] > h]
            rem_set = hours[hours["hour_ending"] > h]
            if rem_fc.empty or rem_set.empty or rem_fc["system_total"].isna().any():
                continue
            F = float(post["peak"])
            F_rem = float(rem_fc["system_total"].max())
            R = float(rem_set["total"].max())
            A = float(hours["total"].max())
            # Intraday, from telemetry of completed hours (live dashboard stand-in).
            th = tel_h.get(d)
            done = (
                th[(th["hour_ending"] <= h) & (th["n"] >= MIN_POINTS_PER_HOUR)]
                if th is not None and h > 0
                else None
            )
            H_tel = float(done["mean_load"].max()) if done is not None and len(done) else math.nan
            if done is not None and len(done):
                m = done.merge(
                    fc[["hour_ending", "repeated", "system_total"]], on=["hour_ending", "repeated"]
                )
                obs_vs_fc = (
                    float((m["mean_load"] / m["system_total"]).mean()) if len(m) else math.nan
                )
                last_ratio = (
                    float(m.iloc[-1]["mean_load"] / m.iloc[-1]["system_total"])
                    if len(m)
                    else math.nan
                )
            else:
                obs_vs_fc = last_ratio = math.nan
            w = tel5[(tel5.index < tdec) & (tel5.index >= tdec - timedelta(minutes=70))]
            cur = float(w.iloc[-1]) if len(w) else math.nan
            ramp = float(w.iloc[-1] - w.iloc[0]) if len(w) > 2 else math.nan
            # Forecast shape and model spread.
            peak_he = int(fc.loc[fc["system_total"].idxmax(), "hour_ending"])
            mods = models_by_doc.get((post["doc_id"], d))
            mp = mods["peak"].to_numpy(float) if mods is not None else np.array([])
            prev = posts.iloc[-2]["peak"] if len(posts) > 1 else math.nan
            # Recent bias: telemetry daily max / day-ahead forecast, days strictly before d.
            ratios = []
            for k in range(1, 15):
                dk = d - timedelta(days=k)
                if dk in tel_max.index and dk in da:
                    ratios.append(float(tel_max[dk]) / da[dk])
                else:
                    ratios.append(math.nan)
            rr = np.array(ratios)
            doy = d.timetuple().tm_yday
            rows.append(
                {
                    "oper_day": d, "hour": h, "t": pd.Timestamp(tdec), "post_utc": post["post_utc"],
                    "F": F, "F_rem": F_rem, "R": R, "A": A, "y": math.log(R / F_rem),
                    "H_tel": H_tel, "H_tel_over_Frem": H_tel / F_rem,
                    "obs_vs_fc": obs_vs_fc, "last_ratio": last_ratio,
                    "cur_over_Frem": cur / F_rem, "ramp_rel": ramp / F_rem,
                    "Frem_over_F": F_rem / F, "hours_left": 24 - h, "peak_he": peak_he,
                    "hours_to_fc_peak": peak_he - h,
                    "rev_rel": (F - prev) / F if not math.isnan(prev) else math.nan,
                    "model_std_rel": float(mp.std() / F) if len(mp) else math.nan,
                    "model_range_rel": float((mp.max() - mp.min()) / F) if len(mp) else math.nan,
                    "model_mean_rel": float(mp.mean() / F) if len(mp) else math.nan,
                    "bias_3": float(np.nanmean(rr[:3])) if np.isfinite(rr[:3]).any() else math.nan,
                    "bias_7": float(np.nanmean(rr[:7])) if np.isfinite(rr[:7]).any() else math.nan,
                    "bias_14": float(np.nanmean(rr)) if np.isfinite(rr).any() else math.nan,
                    "doy_sin": math.sin(2 * math.pi * doy / 365.25),
                    "doy_cos": math.cos(2 * math.pi * doy / 365.25),
                    "dow": d.weekday(), "holiday": int(d in holidays), "F_level": F / 1000.0,
                }
            )  # fmt: skip
        d += timedelta(days=1)
    return pd.DataFrame(rows)


FEATURES = [
    "hour", "F_level", "Frem_over_F", "H_tel_over_Frem", "obs_vs_fc", "last_ratio",
    "cur_over_Frem", "ramp_rel", "hours_left", "peak_he", "hours_to_fc_peak", "rev_rel",
    "model_std_rel", "model_range_rel", "model_mean_rel", "bias_3", "bias_7", "bias_14",
    "doy_sin", "doy_cos", "dow", "holiday",
]  # fmt: skip


def split(ds: pd.DataFrame) -> dict[str, pd.DataFrame]:
    d = ds["oper_day"]
    return {
        "train": ds[d <= TRAIN_END],
        "calib": ds[(d > TRAIN_END) & (d <= CALIB_END)],
        "valid": ds[(d > CALIB_END) & (d <= VALID_END)],
    }


# ---------------------------------------------------------------------------
# Models: each returns a function giving P(R > K) for rows and strikes
# ---------------------------------------------------------------------------


class Baseline:
    """P3A's method: empirical ratio A/F by slot and season, on F (no intraday info)."""

    name = "baseline_p3a"

    def fit(self, train: pd.DataFrame, calib: pd.DataFrame) -> Baseline:
        tr = pd.concat([train, calib])
        self.ref = tr.assign(
            ratio=tr["A"] / tr["F"], doy=[d.timetuple().tm_yday for d in tr["oper_day"]]
        )
        return self

    def prob_above(self, rows: pd.DataFrame, strikes: np.ndarray) -> np.ndarray:
        out = np.empty((len(rows), strikes.shape[1]))
        for i, (_, r) in enumerate(rows.iterrows()):
            g = self.ref[self.ref["hour"] == r["hour"]]
            dd = np.abs(g["doy"].to_numpy() - r["oper_day"].timetuple().tm_yday)
            ratios = g["ratio"].to_numpy()[np.minimum(dd, 365 - dd) <= 30]
            out[i] = [(r["F"] * ratios > k).mean() for k in strikes[i]]
        return out


class NGBoostNormal:
    """The write-up's model: NGBoost, normal on y, with a post-hoc sigma scale."""

    name = "ngboost"

    def fit(self, train: pd.DataFrame, calib: pd.DataFrame) -> NGBoostNormal:
        from ngboost import NGBRegressor  # type: ignore[import-untyped]
        from ngboost.distns import Normal  # type: ignore[import-untyped]

        self.m = NGBRegressor(
            Dist=Normal, n_estimators=600, learning_rate=0.03, verbose=False, random_state=7
        )
        self.m.fit(train[FEATURES].to_numpy(float), train["y"].to_numpy(float))
        mu, sd = self._raw(calib)
        z = (calib["y"].to_numpy(float) - mu) / sd
        self.scale = float(np.sqrt(np.mean(z**2)))  # sigma calibration on held-out calib year
        return self

    def _raw(self, rows: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        dist = self.m.pred_dist(rows[FEATURES].to_numpy(float))
        return np.asarray(dist.params["loc"]), np.asarray(dist.params["scale"])

    def prob_R_above(self, rows: pd.DataFrame, strikes: np.ndarray) -> np.ndarray:
        mu, sd = self._raw(rows)
        z = (np.log(strikes / rows["F_rem"].to_numpy()[:, None]) - mu[:, None]) / (
            sd[:, None] * self.scale
        )
        return np.asarray(1 - norm.cdf(z))


class LGBMConformal:
    """LightGBM quantile regression per quantile, conformalized on the calib year (CQR-style)."""

    name = "lgbm_cqr"

    def fit(self, train: pd.DataFrame, calib: pd.DataFrame) -> LGBMConformal:
        import lightgbm as lgb

        X, y = train[FEATURES].to_numpy(float), train["y"].to_numpy(float)
        self.models = {}
        for q in QUANTILES:
            m = lgb.LGBMRegressor(
                objective="quantile", alpha=q, n_estimators=500, learning_rate=0.03,
                num_leaves=15, min_child_samples=40, subsample=0.8, subsample_freq=1,
                colsample_bytree=0.8, verbose=-1, random_state=7,
            )  # fmt: skip
            m.fit(X, y)
            self.models[q] = m
        # Conformal shift per quantile: make the calib year's empirical coverage match q.
        qc = self._raw(calib)
        yc = calib["y"].to_numpy(float)
        # Shift each quantile by the calib-year residual quantile: P(y <= q_hat + shift) = q.
        self.shift = {q: float(np.quantile(yc - qc[:, j], q)) for j, q in enumerate(QUANTILES)}
        return self

    def _raw(self, rows: pd.DataFrame) -> np.ndarray:
        X = rows[FEATURES].to_numpy(float)
        return np.column_stack([self.models[q].predict(X) for q in QUANTILES])

    def quantiles(self, rows: pd.DataFrame) -> np.ndarray:
        qs = self._raw(rows) + np.array([self.shift[q] for q in QUANTILES])
        return np.sort(qs, axis=1)  # no quantile crossing

    def prob_R_above(self, rows: pd.DataFrame, strikes: np.ndarray) -> np.ndarray:
        qs = self.quantiles(rows)
        levels = np.array(QUANTILES)
        lk = np.log(strikes / rows["F_rem"].to_numpy()[:, None])
        out = np.empty_like(lk)
        for i in range(len(rows)):
            cdf = np.interp(lk[i], qs[i], levels, left=0.0, right=1.0)
            out[i] = 1 - cdf
        return out


# ---------------------------------------------------------------------------
# From P(R > K) to P(A > K), strikes, scores
# ---------------------------------------------------------------------------


def strike_grid(rows: pd.DataFrame) -> np.ndarray:
    base = np.round(rows["F"].to_numpy() / STRIKE_STEP) * STRIKE_STEP
    offs = np.arange(-STRIKE_SPAN, STRIKE_SPAN + 1, STRIKE_STEP)
    return base[:, None] + offs[None, :]


def combine_with_observed(
    rows: pd.DataFrame, p_R: np.ndarray, strikes: np.ndarray, gap: np.ndarray
) -> np.ndarray:
    """P(A > K) = 1 - P(H_settled <= K) P(R <= K); H_settled = H_tel / (1 + g), g from history."""
    H = rows["H_tel"].to_numpy(float)
    out = p_R.copy()
    has = np.isfinite(H)
    if has.any():
        hs = H[has][:, None] / (1 + gap[None, :])  # samples of H_settled
        p_h_le = np.stack([(hs <= k[:, None]).mean(axis=1) for k in strikes[has].T], axis=1)
        out[has] = 1 - p_h_le * (1 - p_R[has])
    return np.clip(out, 0.0, 1.0)


def telemetry_gap(t: Tables, end: date) -> np.ndarray:
    """Telemetry hour mean / settled hour - 1 at each day's settled peak hour, up to `end`."""
    h = t.hourly[t.hourly["oper_day"] <= end]
    peak_rows = h.loc[h.groupby("oper_day")["total"].idxmax()]
    m = peak_rows.merge(t.tel_hourly, on=["oper_day", "hour_ending", "repeated"])
    m = m[m["n"] >= MIN_POINTS_PER_HOUR]
    return (m["mean_load"] / m["total"] - 1).to_numpy(float)


def brier_grid(p: np.ndarray, rows: pd.DataFrame, strikes: np.ndarray) -> np.ndarray:
    y = (rows["A"].to_numpy()[:, None] > strikes).astype(float)
    return (p - y) ** 2


def evaluate(models: dict[str, Any], rows: pd.DataFrame, gap: np.ndarray) -> dict[str, Any]:
    strikes = strike_grid(rows)
    out: dict[str, Any] = {}
    for name, m in models.items():
        if hasattr(m, "prob_R_above"):
            p = combine_with_observed(rows, m.prob_R_above(rows, strikes), strikes, gap)
        else:
            p = m.prob_above(rows, strikes)
        b = brier_grid(p, rows, strikes)
        res: dict[str, Any] = {"brier": float(b.mean()), "by_hour": {}}
        for h in DECISION_HOURS:
            mask = (rows["hour"] == h).to_numpy()
            if mask.any():
                res["by_hour"][h] = float(b[mask].mean())
        hot = rows["A"] >= rows["A"].quantile(0.8)
        res["hot_days"] = float(b[hot.to_numpy()].mean())
        out[name] = res
    return out


def interval_coverage(m: LGBMConformal, rows: pd.DataFrame) -> dict[str, float]:
    qs = m.quantiles(rows)
    y = rows["y"].to_numpy(float)
    lv = list(QUANTILES)
    res = {}
    for lo, hi in ((0.1, 0.9), (0.025, 0.975)):
        i, j = lv.index(lo), lv.index(hi)
        res[f"{round((hi - lo) * 100)}%"] = float(np.mean((y >= qs[:, i]) & (y <= qs[:, j])))
    return res
