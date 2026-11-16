"""Experiment P3A, scoring: ERCOT daily-peak market versus ERCOT's own load forecast.

Implements docs/preregistration.md, P3A, exactly. The scoring machinery (Brier
difference with weekly block bootstrap, the naive taker, its summary) is P2A's,
imported unchanged; only the forecast and the decision rule's capacity clause
are new. Every constant below is a registered value.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from pmw.eval import p2a_score as p2
from pmw.settle import ercot as se

CT = ZoneInfo("America/Chicago")
TRAIN_START = date(2017, 7, 1)
TEST_START = date(2026, 8, 28)  # first operating day with a Kalshi market
TEST_END = date(2026, 11, 14)  # last operating day settled at freezing
SENS_TRAIN_START = date(2023, 8, 28)
DOY_WINDOW = 30
SLOTS = (23, 5, 9, 13, 16)  # Central hour of the postings kept (23 = evening before)
PRIMARY_HOURS = (0, 6, 10, 14)  # Central decision hours on the operating day
LATE_HOUR = 17  # secondary, reported separately
MAX_SPREAD = p2.MAX_SPREAD
SENSITIVITY_SPREADS = p2.SENSITIVITY_SPREADS
N_BOOT = p2.N_BOOT
BOOT_SEED = 20261116
STRESS_SHARE = 0.20
CAPACITY_CONTRACTS = 20_000


# ---------- forecast side ----------


def slot_of(post_utc: datetime, delivery_day: date) -> int | None:
    """Which kept posting slot a posting is, relative to its delivery day."""
    local = post_utc.astimezone(CT)
    if local.date() == delivery_day - timedelta(days=1) and local.hour == 23:
        return 23
    if local.date() == delivery_day and local.hour in SLOTS and local.hour != 23:
        return local.hour
    return None


def in_use_postings(fpeaks: pd.DataFrame) -> pd.DataFrame:
    """One row per (posting, delivery day) of the in-use model, with its slot."""
    f = fpeaks[fpeaks["in_use"]].copy()
    f["post_utc"] = pd.to_datetime(f["post_utc"], utc=True)
    f["slot"] = [
        slot_of(p.to_pydatetime(), d) for p, d in zip(f["post_utc"], f["delivery_day"], strict=True)
    ]
    return f.rename(columns={"peak": "F"})[["delivery_day", "post_utc", "slot", "model", "F"]]


def training_ratios(
    postings: pd.DataFrame, actual: dict[date, float], start: date = TRAIN_START
) -> pd.DataFrame:
    """Error ratios A / F on training days (before the test period), per slot."""
    t = postings[postings["slot"].notna()].copy()
    t = t[(t["delivery_day"] >= start) & (t["delivery_day"] < TEST_START)]
    t["A"] = t["delivery_day"].map(actual)
    t = t[t["A"].notna()]
    # One ratio per (day, slot): the latest posting in that slot.
    t = t.sort_values("post_utc").groupby(["delivery_day", "slot"], as_index=False).last()
    t["ratio"] = t["A"] / t["F"]
    t["doy"] = [d.timetuple().tm_yday for d in t["delivery_day"]]
    return t[["delivery_day", "slot", "doy", "ratio"]]


def _doy_close(doys: np.ndarray, target: int) -> np.ndarray:
    d = np.abs(doys - target)
    return np.minimum(d, 365 - d) <= DOY_WINDOW


def prob_above(F: float, strike: float, ratios: np.ndarray) -> float:
    return float(np.mean(F * ratios > strike))


def decision_times(day: date, hours: tuple[int, ...]) -> list[datetime]:
    return [datetime.combine(day, time(h), tzinfo=CT).astimezone(UTC) for h in hours]


def forecast_at(postings: pd.DataFrame, day: date, t: datetime) -> pd.Series | None:
    """Latest in-use posting for `day` posted strictly before t."""
    p = postings[(postings["delivery_day"] == day) & (postings["post_utc"] < t)]
    if p.empty:
        return None
    return p.sort_values("post_utc").iloc[-1]


# ---------- market side ----------


def load_markets(raw: Path) -> list[dict[str, Any]]:
    return list(json.loads((raw / "markets.json").read_text()))


def load_quotes(raw: Path, tickers: set[str]) -> pd.DataFrame:
    rows = []
    for tk in sorted(tickers):
        path = raw / f"candles_{tk}.json"
        if not path.exists():
            continue
        for c in json.loads(path.read_text())["candlesticks"]:
            rows.append(
                {
                    "ticker": tk,
                    "candle_end": datetime.fromtimestamp(c["end_period_ts"], UTC),
                    "bid": p2._px(c.get("yes_bid"), "close"),
                    "ask": p2._px(c.get("yes_ask"), "close"),
                    "volume": float(c.get("volume", c.get("volume_fp")) or 0.0),
                }
            )
    q = pd.DataFrame(rows, columns=["ticker", "candle_end", "bid", "ask", "volume"])
    q["candle_end"] = pd.to_datetime(q["candle_end"], utc=True)
    return q


# ---------- observations ----------


def build_observations(
    markets: list[dict[str, Any]],
    quotes: pd.DataFrame,
    postings: pd.DataFrame,
    ratios: pd.DataFrame,
    hours: tuple[int, ...] = PRIMARY_HOURS,
    max_spread: float = MAX_SPREAD,
) -> pd.DataFrame:
    """Registered observation set for the given decision hours."""
    by_slot = {s: g for s, g in ratios.groupby("slot")}
    rows = []
    for m in markets:
        if m.get("result") not in ("yes", "no"):
            continue
        day = se.operating_day(m)
        if not TEST_START <= day <= TEST_END:
            continue
        strike = float(m["floor_strike"])
        for h, t in zip(hours, decision_times(day, hours), strict=True):
            f = forecast_at(postings, day, t)
            if f is None or pd.isna(f["slot"]):
                continue
            g = by_slot.get(f["slot"])
            if g is None:
                continue
            r = g["ratio"].to_numpy()[_doy_close(g["doy"].to_numpy(), day.timetuple().tm_yday)]
            if len(r) == 0:
                continue
            rows.append(
                {
                    "t": pd.Timestamp(t),
                    "hour_ct": h,
                    "oper_day": day,
                    "ticker": m["ticker"],
                    "event_ticker": m["event_ticker"],
                    "kind": f"{h:02d}CT",
                    "strike": strike,
                    "F": float(f["F"]),
                    "post_utc": f["post_utc"],
                    "slot": int(f["slot"]),
                    "n_train": len(r),
                    "p_ercot": prob_above(float(f["F"]), strike, r),
                    "y": 1 if m["result"] == "yes" else 0,
                }
            )
    obs = pd.DataFrame(rows)
    if obs.empty:
        return obs
    obs["candle_end"] = obs["t"]
    obs = obs.merge(quotes, on=["ticker", "candle_end"], how="inner")
    two_sided = (obs["bid"] >= 0.01) & (obs["ask"] <= 0.99) & (obs["ask"] > obs["bid"])
    obs = obs[two_sided & ((obs["ask"] - obs["bid"]) <= max_spread + 1e-9)].copy()
    obs["p_mkt"] = (obs["bid"] + obs["ask"]) / 2.0
    obs["spread"] = obs["ask"] - obs["bid"]
    local = obs["t"].dt.tz_convert(CT)
    iso = local.dt.isocalendar()
    obs["week"] = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
    return obs.sort_values(["t", "ticker"]).reset_index(drop=True)


def leakage_violations(obs: pd.DataFrame) -> dict[str, int]:
    return {
        "posting_not_before_decision": int((obs["post_utc"] >= obs["t"]).sum()),
        "training_day_in_test": 0,  # enforced in training_ratios by construction
    }


# ---------- capacity ----------


def capacity(markets: list[dict[str, Any]], actual: dict[date, float]) -> dict[str, Any]:
    per_day: dict[date, dict[str, float]] = {}
    for m in markets:
        day = se.operating_day(m)
        if not TEST_START <= day <= TEST_END:
            continue
        d = per_day.setdefault(day, {"contracts": 0.0, "premium": 0.0})
        v = float(m.get("volume_fp") or m.get("volume") or 0.0)
        d["contracts"] += v
        last = m.get("last_price_dollars")
        d["premium"] += v * float(last) if last not in (None, "") else 0.0
    days = sorted(d for d in per_day if d in actual)
    if not days:
        return {"days": 0, "gate_met": False}
    k = max(1, math.ceil(STRESS_SHARE * len(days)))
    stressed = sorted(days, key=lambda d: -actual[d])[:k]
    c = [per_day[d]["contracts"] for d in stressed]
    return {
        "days": len(days),
        "stressed_days": [str(d) for d in sorted(stressed)],
        "median_contracts_stressed": float(np.median(c)),
        "median_contracts_all": float(np.median([per_day[d]["contracts"] for d in days])),
        "median_premium_stressed_approx": float(
            np.median([per_day[d]["premium"] for d in stressed])
        ),
        "gate_met": bool(np.median(c) >= CAPACITY_CONTRACTS),
    }


# ---------- decision ----------


def decide(
    primary: p2.DiffResult, pnl_share: float, sensitivity: list[p2.DiffResult], capacity_ok: bool
) -> str:
    signs = {math.copysign(1, primary.d)} | {math.copysign(1, s.d) for s in sensitivity}
    if len(signs) > 1:
        return "CONDITIONAL"
    if primary.ci_low > 0 and pnl_share >= p2.GO_PNL_SHARE and capacity_ok:
        return "GO"
    if primary.ci_high < 0 and pnl_share < p2.NOGO_PNL_SHARE:
        return "NO GO"
    return "CONDITIONAL"


def brier_difference(
    obs: pd.DataFrame, col: str = "p_ercot", n_boot: int = N_BOOT
) -> p2.DiffResult:
    y = obs["y"].to_numpy(float)
    diff = p2.brier(obs["p_mkt"].to_numpy(float), y) - p2.brier(obs[col].to_numpy(float), y)
    boot = p2.block_bootstrap_mean(diff, obs["week"].to_numpy(), n_boot=n_boot, seed=BOOT_SEED)
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return p2.DiffResult(len(obs), obs["week"].nunique(), float(diff.mean()), float(lo), float(hi))


def score(
    markets: list[dict[str, Any]],
    quotes: pd.DataFrame,
    postings: pd.DataFrame,
    actual: dict[date, float],
    n_boot: int = N_BOOT,
) -> dict[str, Any]:
    ratios = training_ratios(postings, actual)
    ratios_recent = training_ratios(postings, actual, start=SENS_TRAIN_START)
    obs = build_observations(markets, quotes, postings, ratios)
    primary = brier_difference(obs, n_boot=n_boot)
    sens_spread = [
        brier_difference(
            build_observations(markets, quotes, postings, ratios, max_spread=s), n_boot=n_boot
        )
        for s in SENSITIVITY_SPREADS
    ]
    sens_train = brier_difference(
        build_observations(markets, quotes, postings, ratios_recent), n_boot=n_boot
    )
    trades = {tau: p2.simulate_taker(obs, tau, prob_col="p_ercot") for tau in p2.TAUS}
    taker = {str(tau): p2.taker_summary(obs, tr, n_boot=n_boot) for tau, tr in trades.items()}
    cap = capacity(markets, actual)
    pnl_share = taker[str(p2.PRIMARY_TAU)]["share_resamples_positive"]
    verdict = decide(primary, pnl_share, [*sens_spread, sens_train], cap["gate_met"])

    y = obs["y"].to_numpy(float)
    pm, pe = obs["p_mkt"].to_numpy(float), obs["p_ercot"].to_numpy(float)
    by_hour = {
        f"{h:02d}CT": brier_difference(obs[obs["hour_ct"] == h], n_boot=n_boot).as_dict()
        for h in PRIMARY_HOURS
        if (obs["hour_ct"] == h).any()
    }
    days = sorted(set(obs["oper_day"]))
    k = max(1, math.ceil(STRESS_SHARE * len(days)))
    top_days = set(sorted(days, key=lambda d: -actual.get(d, 0.0))[:k])
    top = obs[obs["oper_day"].isin(top_days)]
    late = build_observations(markets, quotes, postings, ratios, hours=(LATE_HOUR,))
    return {
        "verdict": verdict,
        "primary": primary.as_dict(),
        "sensitivity_spread": {
            str(s): r.as_dict() for s, r in zip(SENSITIVITY_SPREADS, sens_spread, strict=True)
        },
        "sensitivity_train_recent": sens_train.as_dict(),
        "brier": {"market": float(p2.brier(pm, y).mean()), "ercot": float(p2.brier(pe, y).mean())},
        "logloss": {
            "market": float(p2.logloss(pm, y).mean()),
            "ercot": float(p2.logloss(pe, y).mean()),
        },
        "murphy": {"market": p2.murphy(pm, y), "ercot": p2.murphy(pe, y)},
        "reliability": {
            "market": p2.reliability_table(pm, y),
            "ercot": p2.reliability_table(pe, y),
        },
        "by_hour": by_hour,
        "top_days": brier_difference(top, n_boot=n_boot).as_dict() if len(top) else None,
        "late_17CT": brier_difference(late, n_boot=n_boot).as_dict() if len(late) else None,
        "taker": taker,
        "capacity": cap,
        "leakage": leakage_violations(obs),
        "n_markets": int(obs["ticker"].nunique()),
        "n_days": len(days),
        "training": {"ratios": len(ratios), "ratios_recent": len(ratios_recent)},
    }


# ---------- synthetic known-answer world ----------


def synthetic_world(
    ercot_noise: float, market_noise: float, seed: int = 7, volume: float = 5000.0
) -> tuple[list[dict[str, Any]], pd.DataFrame, pd.DataFrame, dict[date, float]]:
    """A world where the true peak is known and both forecasts are noisy versions of it.

    Actual peak A = F_true x (1 + e). ERCOT's forecast F = F_true x (1 + ercot_noise
    noise); the market's belief is the correct probability under its own noisy view
    of F_true. Training days get the same ERCOT noise, so the ERCOT probability is
    calibrated by construction; the one with less noise should win.
    """
    rng = np.random.default_rng(seed)
    sigma = 0.03
    actual: dict[date, float] = {}
    posts, markets, quotes = [], [], []
    d = TRAIN_START
    while d <= TEST_END:
        base = 60000 + 15000 * math.sin(2 * math.pi * (d.timetuple().tm_yday - 100) / 365)
        A = base * (1 + rng.normal(0, sigma))
        actual[d] = A
        for slot in SLOTS:
            post_day = d - timedelta(days=1) if slot == 23 else d
            post = datetime.combine(post_day, time(slot, 30), tzinfo=CT).astimezone(UTC)
            F = A / (1 + rng.normal(0, ercot_noise))
            posts.append(
                {"delivery_day": d, "post_utc": post, "model": "X", "in_use": True, "peak": F}
            )
        if d >= TEST_START:
            et = f"KXTXERCOTPEAKD-{(d + timedelta(days=1)):%y%b%d}".upper()
            for j, K in enumerate(np.arange(-3, 4) * 1500 + round(base, -3)):
                tk = f"{et}-T{int(K)}"
                markets.append(
                    {
                        "ticker": tk, "event_ticker": et, "floor_strike": float(K),
                        "strike_type": "greater", "result": "yes" if A > K else "no",
                        "volume_fp": str(volume), "last_price_dollars": "0.5",
                        "rules_primary": f"If the highest hourly total system load reported for "
                        f"the ERCOT grid on {d:%B} {d.day}, {d.year} is above {int(K)} MW, then "
                        "the market resolves to Yes.",
                    }
                )  # fmt: skip
                for h in (*PRIMARY_HOURS, LATE_HOUR):
                    t = datetime.combine(d, time(h), tzinfo=CT).astimezone(UTC)
                    view = A / (1 + rng.normal(0, market_noise))
                    sd = math.hypot(market_noise, sigma) * view
                    p = 0.5 * math.erfc((K - view) / (sd * math.sqrt(2)))
                    p = min(max(p, 0.03), 0.97)
                    quotes.append(
                        {"ticker": tk, "candle_end": t, "bid": round(p - 0.02, 2),
                         "ask": round(p + 0.02, 2), "volume": 2000.0}
                    )  # fmt: skip
                _ = j
        d += timedelta(days=1)
    fpeaks = pd.DataFrame(posts)
    q = pd.DataFrame(quotes)
    q["candle_end"] = pd.to_datetime(q["candle_end"], utc=True)
    return markets, q, in_use_postings(fpeaks), actual


# ---------- run and report ----------


def load_inputs(
    raw: Path, interim: Path
) -> tuple[list[dict[str, Any]], pd.DataFrame, pd.DataFrame, dict[date, float]]:
    markets = load_markets(raw)
    quotes = load_quotes(raw, {m["ticker"] for m in markets})
    postings = in_use_postings(pd.read_parquet(interim / "forecast_peaks.parquet"))
    daily = pd.read_parquet(interim / "daily_peak.parquet")
    actual = {
        (d.date() if hasattr(d, "date") else d): float(v)
        for d, v in zip(daily["oper_day"], daily["peak_first_complete"], strict=True)
        if v is not None
    }
    return markets, quotes, postings, actual


def _row(name: str, r: dict[str, Any] | None) -> str:
    if not r:
        return f"| {name} | - | - | - | - |"
    return f"| {name} | {r['n']} | {r['weeks']} | {r['D']:+.4f} | [{r['ci_low']:+.4f}, {r['ci_high']:+.4f}] |"  # noqa: E501


def report_markdown(r: dict[str, Any]) -> str:
    t = r["taker"]
    lines = [
        "# P3A result tables",
        "",
        f"Verdict under the registered rule: **{r['verdict']}**",
        "",
        "D = Brier(market) - Brier(ERCOT forecast); positive means the ERCOT forecast was more accurate.",  # noqa: E501
        "",
        "| | n | weeks | D | 95% CI |",
        "| - | -: | -: | -: | - |",
        _row("**Primary**", r["primary"]),
        *[_row(f"Spread <= {k}", v) for k, v in r["sensitivity_spread"].items()],
        _row("Training on last 3 years", r["sensitivity_train_recent"]),
        *[_row(f"Decision {k}", v) for k, v in r["by_hour"].items()],
        _row("Top 20% peak days", r["top_days"]),
        _row("17:00 CT (secondary)", r["late_17CT"]),
        "",
        "| Forecast | Brier | Log loss |",
        "| - | -: | -: |",
        f"| Kalshi market | {r['brier']['market']:.4f} | {r['logloss']['market']:.4f} |",
        f"| ERCOT forecast | {r['brier']['ercot']:.4f} | {r['logloss']['ercot']:.4f} |",
        "",
        "Murphy decomposition (reliability, resolution, uncertainty):",
        "",
        *[
            f"- {k}: {v['reliability']:.4f}, {v['resolution']:.4f}, {v['uncertainty']:.4f}"
            for k, v in r["murphy"].items()
        ],
        "",
        "| Taker tau | trades | contracts | P&L after fees | fees | share of resamples > 0 | 95% interval | perceived/realized corr |",  # noqa: E501
        "| -: | -: | -: | -: | -: | -: | - | -: |",
    ]
    for tau, s in t.items():
        if not s.get("trades"):
            lines.append(f"| {tau} | 0 | 0 | 0 | 0 | 0 | - | - |")
            continue
        lines.append(
            f"| {tau} | {s['trades']} | {s['contracts']} | ${s['pnl']:,.0f} | ${s['fees']:,.0f} | "
            f"{s['share_resamples_positive']:.3f} | [${s['pnl_ci'][0]:,.0f}, ${s['pnl_ci'][1]:,.0f}] | "  # noqa: E501
            f"{s['perceived_vs_realized_corr']:.2f} |"
        )
    c = r["capacity"]
    lines += [
        "",
        f"Capacity: median contracts per stressed day {c.get('median_contracts_stressed', 0):,.0f} "
        f"(gate 20,000: {'met' if c.get('gate_met') else 'not met'}); all days median "
        f"{c.get('median_contracts_all', 0):,.0f}; approximate premium on stressed days "
        f"${c.get('median_premium_stressed_approx', 0):,.0f}. Stressed days: {', '.join(c.get('stressed_days', []))}.",  # noqa: E501
        "",
        f"Leakage check: {r['leakage']}. Markets scored: {r['n_markets']}; operating days: {r['n_days']}; "  # noqa: E501
        f"training ratios: {r['training']}.",
        "",
        "Reliability (bin, n, mean forecast, observed frequency):",
        "",
    ]
    for k, rows in r["reliability"].items():
        lines.append(
            f"- {k}: " + "; ".join(f"{x['mean_p']:.2f}->{x['freq']:.2f} (n={x['n']})" for x in rows)
        )
    return "\n".join(lines) + "\n"
