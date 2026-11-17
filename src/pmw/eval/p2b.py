"""Experiment P2B: calibration of NBM snowfall probabilities against CLI snowfall.

Registered in docs/preregistration.md (P2B). No prices. Forecasts are the P2A
composite (NBM 24-hour percentiles mapped onto climate days) at leads of 1 to 7
days before a window of 1 to 3 climate days; outcomes are CLI daily snowfall
(ACIS where CLI is missing).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from pmw.eval import p2a_score as p2
from pmw.models.nbm_composite import build_composite
from pmw.utils.timeutil import climate_day_bounds_utc

WINDOWS = (1, 2, 3)
LEADS = (1, 2, 3, 5, 7)
THRESHOLDS = (0.1, 1.0, 2.0, 4.0, 6.0, 8.0, 12.0)
DEV_SEASONS = (2022, 2023, 2024, 2025)  # labelled by the January of the winter
TEST_SEASON = 2026
BOOT_SEED = 20261118
CLIP = 1e-3


def season_days(season: int) -> tuple[date, date]:
    return date(season - 1, 11, 1), date(season, 3, 31)


@dataclass
class StationInputs:
    icao: str
    std_offset_h: float
    nbp: Any
    nbs: Any
    observed: dict[date, Any]
    climatology: Any


# ---------- cases (forecasts and outcomes) ----------


def build_cases(st: StationInputs, seasons: tuple[int, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for season in seasons:
        first, last = season_days(season)
        d = first
        while d <= last:
            for n in WINDOWS:
                window = [d + timedelta(days=i) for i in range(n)]
                if window[-1] > last:
                    continue
                vals = [getattr(st.observed.get(x), "snow_in", None) for x in window]
                if any(v is None for v in vals):
                    continue
                total = float(sum(float(v) for v in vals if v is not None))
                start_utc = climate_day_bounds_utc(d, st.std_offset_h)[0]
                for lead in LEADS:
                    t = start_utc - timedelta(days=lead)
                    kw: dict[str, Any] = dict(
                        window=window, std_offset_h=st.std_offset_h, t=t,
                        observed=st.observed, climatology=st.climatology,
                        seed_key=f"{st.icao}{d}{n}{lead}",
                    )  # fmt: skip
                    c = build_composite(nbp=st.nbp, nbs=st.nbs, **kw)
                    climo_w = float(sum(p.weight for p in c.climo))
                    if climo_w > 0 or not c.forecast or c.observed_days:
                        continue
                    cl = build_composite(nbp=None, nbs=None, **kw)
                    row: dict[str, Any] = {
                        "station": st.icao, "season": season, "start": d, "n": n,
                        "lead": lead, "t": t, "total": total,
                        "versions": ",".join(sorted({p.version for p in c.forecast})),
                        "max_available_at": max(p.available_at for p in c.forecast),
                        "n_observed_at_t": len(c.observed_days),
                    }  # fmt: skip
                    for k in THRESHOLDS:
                        row[f"p_{k:g}"] = c.prob_above(k)
                        row[f"c_{k:g}"] = cl.prob_above(k)
                    rows.append(row)
            d += timedelta(days=1)
    return rows


def leakage(cases: pd.DataFrame) -> dict[str, int]:
    t = pd.to_datetime(cases["t"], utc=True)
    avail = pd.to_datetime(cases["max_available_at"], utc=True)
    return {
        "nbm_not_public_before_t": int((avail >= t).sum()),
        "window_day_observed_at_t": int((cases["n_observed_at_t"] > 0).sum()),
    }


def long_form(cases: pd.DataFrame) -> pd.DataFrame:
    """One row per (case, threshold): p (NBM), c (climatology), y."""
    parts = []
    for k in THRESHOLDS:
        part = cases[["station", "season", "start", "n", "lead", "versions", "total"]].copy()
        part["k"] = k
        part["p"] = cases[f"p_{k:g}"].to_numpy(float)
        part["c"] = cases[f"c_{k:g}"].to_numpy(float)
        part["y"] = (cases["total"].to_numpy(float) > k + 1e-9).astype(float)
        parts.append(part)
    out = pd.concat(parts, ignore_index=True)
    iso = pd.to_datetime(out["start"]).dt.isocalendar()
    out["week"] = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
    return out


# ---------- recalibration ----------


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, CLIP, 1 - CLIP)
    return np.log(p / (1 - p))


def fit_recalibration(p: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """(a, b) maximizing the Bernoulli likelihood of sigmoid(a + b logit p)."""
    x = _logit(p)

    def nll(w: np.ndarray) -> float:
        z = w[0] + w[1] * x
        return float(np.mean(np.logaddexp(0, z) - y * z))

    res = minimize(nll, np.array([0.0, 1.0]), method="L-BFGS-B")
    return float(res.x[0]), float(res.x[1])


def apply_recalibration(p: np.ndarray, ab: tuple[float, float]) -> np.ndarray:
    return np.asarray(1 / (1 + np.exp(-(ab[0] + ab[1] * _logit(p)))))


def fit_all(dev: pd.DataFrame) -> dict[tuple[float, int], tuple[float, float]]:
    out = {}
    for (k, n), g in dev.groupby(["k", "n"]):
        out[(float(k), int(n))] = fit_recalibration(g["p"].to_numpy(), g["y"].to_numpy())
    return out


def recalibrated(
    df: pd.DataFrame, coefs: dict[tuple[float, int], tuple[float, float]]
) -> np.ndarray:
    out = np.empty(len(df))
    for (k, n), idx in df.groupby(["k", "n"]).groups.items():
        pos = df.index.get_indexer(idx)
        out[pos] = apply_recalibration(df.loc[idx, "p"].to_numpy(), coefs[(float(k), int(n))])
    return out


# ---------- scores ----------


def describe(df: pd.DataFrame) -> dict[str, Any]:
    y, p, c = df["y"].to_numpy(), df["p"].to_numpy(), df["c"].to_numpy()
    bn, bc = float(np.mean((p - y) ** 2)), float(np.mean((c - y) ** 2))
    return {
        "n": len(df),
        "base_rate": round(float(y.mean()), 4),
        "mean_p": round(float(p.mean()), 4),
        "brier": round(bn, 5),
        "brier_climo": round(bc, 5),
        "bss": round(1 - bn / bc, 3) if bc > 0 else None,
    }


def recal_test(test: pd.DataFrame, p_recal: np.ndarray, n_boot: int = p2.N_BOOT) -> dict[str, Any]:
    y = test["y"].to_numpy()
    diff = (test["p"].to_numpy() - y) ** 2 - (p_recal - y) ** 2
    boot = p2.block_bootstrap_mean(diff, test["week"].to_numpy(), n_boot=n_boot, seed=BOOT_SEED)
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return {
        "n": len(test),
        "weeks": int(test["week"].nunique()),
        "dB": float(diff.mean()),
        "ci_low": float(lo),
        "ci_high": float(hi),
        "weakness_documented": bool(lo > 0),
    }


def score(dev: pd.DataFrame, test: pd.DataFrame, n_boot: int = p2.N_BOOT) -> dict[str, Any]:
    coefs = fit_all(dev)
    p_recal = recalibrated(test, coefs)
    res: dict[str, Any] = {
        "primary": recal_test(test, p_recal, n_boot),
        "coefficients": {
            f"K{k:g}_n{n}": [round(a, 3), round(b, 3)] for (k, n), (a, b) in coefs.items()
        },
    }
    t = test.assign(p_recal=p_recal)
    for col in ("k", "lead", "n"):
        res[f"test_by_{col}"] = {
            str(v): recal_test(g, g["p_recal"].to_numpy(), n_boot) for v, g in t.groupby(col)
        }
    res["dev_by_k_lead"] = {
        f"K{k:g}_L{lead}": describe(g) for (k, lead), g in dev.groupby(["k", "lead"])
    }
    res["dev_by_k_version"] = {
        f"K{k:g}_{v}": describe(g) for (k, v), g in dev.groupby(["k", "versions"]) if len(g) >= 200
    }
    res["dev_reliability_by_k"] = {
        f"K{k:g}": p2.reliability_table(g["p"].to_numpy(), g["y"].to_numpy())
        for k, g in dev.groupby("k")
    }
    res["test_describe_by_k"] = {f"K{k:g}": describe(g) for k, g in test.groupby("k")}
    res["test_recal_brier_by_k"] = {
        f"K{k:g}": round(float(np.mean((g["p_recal"] - g["y"]) ** 2)), 5) for k, g in t.groupby("k")
    }
    return res


# ---------- synthetic known-answer world ----------


def synthetic(
    overconfident: bool, seed: int = 5, weeks: int = 40
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Cases where the truth is known; NBM is either calibrated or overconfident."""
    rng = np.random.default_rng(seed)

    def make(season: int) -> pd.DataFrame:
        rows = []
        for w in range(weeks):
            for j in range(60):
                q = float(rng.beta(0.6, 3.0))
                y = float(rng.random() < q)
                z = math.log(max(q, 1e-4) / max(1 - q, 1e-4))
                p = 1 / (1 + math.exp(-(2.2 * z if overconfident else z)))
                rows.append(
                    {"k": 1.0, "n": 1, "lead": 1 + j % 3, "p": p, "c": 0.15, "y": y,
                     "versions": "V4.2", "week": f"{season}-W{w:02d}"}
                )  # fmt: skip
        return pd.DataFrame(rows)

    return make(2022), make(2026)


def load_station_inputs(
    icao: str, std_offset_h: float, root: Path = Path("data/interim")
) -> StationInputs:
    from pmw.eval.p2a import load_climatology, load_nbp, load_nbs, load_observed

    return StationInputs(
        icao, std_offset_h,
        load_nbp(root / "nbm/nbp_periods.parquet", icao),
        load_nbs(root / "nbm/nbs_s06.parquet", icao),
        load_observed(root / "obs/daily_snow_p2b.parquet", icao),
        load_climatology(root / "climo/daily_snow.parquet", icao),
    )  # fmt: skip


def run(stations: dict[str, float], seasons: tuple[int, ...]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for icao, off in sorted(stations.items()):
        rows += build_cases(load_station_inputs(icao, off), seasons)
    return pd.DataFrame(rows)
