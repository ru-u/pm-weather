"""Experiment P2A, stage 2: join quotes and outcomes, score, simulate, decide.

Implements docs/preregistration.md (P2A and its implementation clarifications)
exactly. Nothing here is tuned; every number below is a registered value.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pmw.markets.fees import fill_fee

MAX_SPREAD = 0.20
SENSITIVITY_SPREADS = (0.10, 0.30)
N_BOOT = 10_000
BOOT_SEED = 20261115
LOGLOSS_CLIP = (0.01, 0.99)
TAUS = (0.05, 0.10, 0.15)
PRIMARY_TAU = 0.10
VOLUME_SHARE = 0.10
MARKET_DAY_CAP = 250
EVENT_DAY_CAP = 1000
GO_PNL_SHARE = 0.90
NOGO_PNL_SHARE = 0.50
LEAD_BANDS = ((0.0, 3.0, "< 3 d"), (3.0, 9.0, "3 to 9 d"), (9.0, 1e9, "> 9 d"))


# ---------- inputs ----------


def _px(node: Any, key: str) -> float | None:
    if not node:
        return None
    v = node.get(key, node.get(f"{key}_dollars"))
    return None if v in (None, "") else float(v)


def load_quotes(raw_dir: Path, tickers: set[str]) -> pd.DataFrame:
    """Hourly candle closes: one row per (ticker, candle end)."""
    rows = []
    for tk in sorted(tickers):
        path = raw_dir / f"candles_{tk}.json"
        if not path.exists():
            continue
        for c in json.loads(path.read_text()):
            rows.append(
                {
                    "ticker": tk,
                    "candle_end": datetime.fromtimestamp(c["end_period_ts"], UTC),
                    "bid": _px(c.get("yes_bid"), "close"),
                    "ask": _px(c.get("yes_ask"), "close"),
                    "volume": float(c.get("volume", c.get("volume_fp")) or 0.0),
                }
            )
    return pd.DataFrame(rows)


def load_outcomes(markets_path: Path) -> dict[str, int]:
    return {
        m["ticker"]: 1 if m["result"] == "yes" else 0
        for m in json.loads(markets_path.read_text())
        if m.get("result") in ("yes", "no")
    }


def window_ends(markets_path: Path, events: list[Any]) -> dict[str, datetime]:
    from pmw.utils.timeutil import climate_day_bounds_utc

    return {
        ev.event_ticker: climate_day_bounds_utc(ev.window[-1], ev.station.std_utc_offset_hours)[1]
        for ev in events
    }


def assemble(
    forecasts: pd.DataFrame,
    quotes: pd.DataFrame,
    outcomes: dict[str, int],
    window_end: dict[str, datetime],
    max_spread: float = MAX_SPREAD,
) -> pd.DataFrame:
    """Registered observation set: undetermined, with a usable two-sided quote."""
    f = forecasts[~forecasts["determined"]].copy()
    f["candle_end"] = f["t"].dt.floor("h")
    q = quotes.copy()
    obs = f.merge(q, on=["ticker", "candle_end"], how="inner")
    two_sided = (obs["bid"] >= 0.01) & (obs["ask"] <= 0.99) & (obs["ask"] > obs["bid"])
    obs = obs[two_sided & ((obs["ask"] - obs["bid"]) <= max_spread + 1e-9)].copy()
    obs["p_mkt"] = (obs["bid"] + obs["ask"]) / 2.0
    obs["spread"] = obs["ask"] - obs["bid"]
    obs["y"] = obs["ticker"].map(outcomes)
    obs = obs[obs["y"].notna()].copy()
    obs["y"] = obs["y"].astype(int)
    iso = obs["t"].dt.isocalendar()
    obs["week"] = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
    ends = obs["event_ticker"].map(window_end)
    obs["lead_days"] = (pd.to_datetime(ends, utc=True) - obs["t"]).dt.total_seconds() / 86400.0
    obs["lead_band"] = pd.cut(
        obs["lead_days"],
        bins=[b[0] for b in LEAD_BANDS] + [LEAD_BANDS[-1][1]],
        labels=[b[2] for b in LEAD_BANDS],
        right=False,
    ).astype(str)
    obs["version"] = obs["nbm_versions"].map(
        lambda v: v if v and "," not in v else ("mixed" if v else "none")
    )
    return obs.sort_values(["t", "ticker"]).reset_index(drop=True)


# ---------- scores ----------


def brier(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    return (p - y) ** 2


def logloss(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    q = np.clip(p, *LOGLOSS_CLIP)
    return -(y * np.log(q) + (1 - y) * np.log(1 - q))


def murphy(p: np.ndarray, y: np.ndarray, bins: int = 10) -> dict[str, float]:
    """Brier = reliability - resolution + uncertainty (binned)."""
    idx = np.minimum((p * bins).astype(int), bins - 1)
    n = len(p)
    obar = y.mean()
    rel = res = 0.0
    for k in range(bins):
        m = idx == k
        if m.any():
            nk = m.sum()
            rel += nk * (p[m].mean() - y[m].mean()) ** 2
            res += nk * (y[m].mean() - obar) ** 2
    return {"reliability": rel / n, "resolution": res / n, "uncertainty": obar * (1 - obar)}


def reliability_table(p: np.ndarray, y: np.ndarray, bins: int = 10) -> list[dict[str, float]]:
    idx = np.minimum((p * bins).astype(int), bins - 1)
    out = []
    for k in range(bins):
        m = idx == k
        if m.any():
            out.append(
                {
                    "bin": k / bins,
                    "n": int(m.sum()),
                    "mean_p": float(p[m].mean()),
                    "freq": float(y[m].mean()),
                }
            )
    return out


def block_bootstrap_mean(
    values: np.ndarray, blocks: np.ndarray, n_boot: int = N_BOOT, seed: int = BOOT_SEED
) -> np.ndarray:
    """Observation-weighted mean of `values` under resampling of whole blocks."""
    keys, inv = np.unique(blocks, return_inverse=True)
    sums = np.bincount(inv, weights=values, minlength=len(keys))
    counts = np.bincount(inv, minlength=len(keys)).astype(float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(keys), size=(n_boot, len(keys)))
    return np.asarray(sums[idx].sum(axis=1) / counts[idx].sum(axis=1))


def block_bootstrap_total(
    values: np.ndarray, blocks: np.ndarray, n_boot: int = N_BOOT, seed: int = BOOT_SEED
) -> np.ndarray:
    keys, inv = np.unique(blocks, return_inverse=True)
    sums = np.bincount(inv, weights=values, minlength=len(keys))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(keys), size=(n_boot, len(keys)))
    return np.asarray(sums[idx].sum(axis=1))


@dataclass
class DiffResult:
    n: int
    weeks: int
    d: float
    ci_low: float
    ci_high: float

    def as_dict(self) -> dict[str, float]:
        return {
            "n": self.n,
            "weeks": self.weeks,
            "D": self.d,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
        }


def brier_difference(obs: pd.DataFrame, col: str = "p_nbm", n_boot: int = N_BOOT) -> DiffResult:
    """D = Brier(market) - Brier(forecast). Positive: the forecast is more accurate."""
    y = obs["y"].to_numpy(float)
    diff = brier(obs["p_mkt"].to_numpy(float), y) - brier(obs[col].to_numpy(float), y)
    boot = block_bootstrap_mean(diff, obs["week"].to_numpy(), n_boot=n_boot)
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return DiffResult(len(obs), obs["week"].nunique(), float(diff.mean()), float(lo), float(hi))


# ---------- taker simulation ----------


def simulate_taker(obs: pd.DataFrame, tau: float, prob_col: str = "p_nbm") -> pd.DataFrame:
    """Registered naive taker: trades with after-fee perceived edge above tau."""
    trades = []
    held: set[tuple[str, Any]] = set()
    event_day_used: dict[tuple[str, Any], int] = {}
    for _, grp in obs.groupby(["t", "event_ticker"], sort=True):
        cands = []
        for r in grp.itertuples(index=False):
            day = r.t.date()
            if (r.ticker, day) in held:
                continue
            size = math.floor(min(VOLUME_SHARE * r.volume, MARKET_DAY_CAP))
            if size < 1:
                continue
            p = float(getattr(r, prob_col))
            fee_yes = float(fill_fee(Decimal(str(r.ask)), size).total) / size
            fee_no = float(fill_fee(Decimal(str(round(1 - r.bid, 4))), size).total) / size
            edge_yes = p - r.ask - fee_yes
            edge_no = r.bid - p - fee_no
            if edge_yes > tau:
                cands.append((edge_yes, r, "yes", r.ask, fee_yes, size, p - r.ask))
            elif edge_no > tau:
                cands.append((edge_no, r, "no", 1 - r.bid, fee_no, size, r.bid - p))
        for _edge, r, side, price, fee, size, perceived in sorted(cands, key=lambda c: -c[0]):
            key = (r.event_ticker, r.t.date())
            room = EVENT_DAY_CAP - event_day_used.get(key, 0)
            n = min(size, room)
            if n < 1:
                continue
            if n != size:
                fee = float(fill_fee(Decimal(str(round(price, 4))), n).total) / n
            win = r.y if side == "yes" else 1 - r.y
            realized = win - price
            trades.append(
                {
                    "t": r.t, "week": r.week, "ticker": r.ticker, "event_ticker": r.event_ticker,
                    "kind": r.kind, "side": side, "price": price, "contracts": n, "fee_per": fee,
                    "perceived_edge": perceived, "realized_edge": realized,
                    "pnl": n * (realized - fee),
                }
            )  # fmt: skip
            event_day_used[key] = event_day_used.get(key, 0) + n
            held.add((r.ticker, r.t.date()))
    return pd.DataFrame(trades)


def taker_summary(obs: pd.DataFrame, trades: pd.DataFrame, n_boot: int = N_BOOT) -> dict[str, Any]:
    weeks = obs["week"].unique()
    if trades.empty:
        return {"trades": 0, "pnl": 0.0, "share_resamples_positive": 0.0}
    per_week = trades.groupby("week")["pnl"].sum().reindex(weeks, fill_value=0.0)
    boot = block_bootstrap_total(
        per_week.to_numpy(float), np.asarray(per_week.index), n_boot=n_boot
    )
    cum = trades.sort_values("t")["pnl"].cumsum().to_numpy()
    drawdown = (
        float(np.max(np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:] - cum))
        if len(cum)
        else 0.0
    )
    corr = (
        trades[["perceived_edge", "realized_edge"]].corr().iloc[0, 1]
        if len(trades) > 2
        else float("nan")
    )
    return {
        "trades": len(trades),
        "contracts": int(trades["contracts"].sum()),
        "pnl": float(trades["pnl"].sum()),
        "fees": float((trades["fee_per"] * trades["contracts"]).sum()),
        "pnl_per_contract": float(trades["pnl"].sum() / trades["contracts"].sum()),
        "share_resamples_positive": float(np.mean(boot > 0)),
        "pnl_ci": [float(x) for x in np.quantile(boot, [0.025, 0.975])],
        "max_drawdown": drawdown,
        "weeks_with_trades": int(trades["week"].nunique()),
        "events_traded": int(trades["event_ticker"].nunique()),
        "perceived_vs_realized_corr": float(corr),
        "by_kind": {k: float(v) for k, v in trades.groupby("kind")["pnl"].sum().items()},
    }


# ---------- the decision ----------


def decide(primary: DiffResult, pnl_share: float, sensitivity: list[DiffResult]) -> str:
    """Registered decision rule."""
    signs = {math.copysign(1, primary.d)} | {math.copysign(1, s.d) for s in sensitivity}
    if len(signs) > 1:
        return "CONDITIONAL"
    if primary.ci_low > 0 and pnl_share >= GO_PNL_SHARE:
        return "GO"
    if primary.ci_high < 0 and pnl_share < NOGO_PNL_SHARE:
        return "NO GO"
    return "CONDITIONAL"


def score_all(
    forecasts: pd.DataFrame,
    quotes: pd.DataFrame,
    outcomes: dict[str, int],
    window_end: dict[str, datetime],
    n_boot: int = N_BOOT,
    simulate: Callable[..., pd.DataFrame] | None = None,
) -> dict[str, Any]:
    """`simulate` replaces the candle taker (P2A-S fills from logged books)."""
    simulate = simulate or simulate_taker
    obs = assemble(forecasts, quotes, outcomes, window_end)
    y = obs["y"].to_numpy(float)
    primary = brier_difference(obs, "p_nbm", n_boot)
    sens = [
        brier_difference(assemble(forecasts, quotes, outcomes, window_end, s), "p_nbm", n_boot)
        for s in SENSITIVITY_SPREADS
    ]
    trades = {tau: simulate(obs, tau) for tau in TAUS}
    summaries = {tau: taker_summary(obs, tr, n_boot) for tau, tr in trades.items()}
    verdict = decide(primary, summaries[PRIMARY_TAU].get("share_resamples_positive", 0.0), sens)
    out: dict[str, Any] = {
        "verdict": verdict,
        "primary": primary.as_dict(),
        "sensitivity_spread": {
            str(s): r.as_dict() for s, r in zip(SENSITIVITY_SPREADS, sens, strict=True)
        },
        "nbm_independent_days": brier_difference(obs, "p_nbm_indep", n_boot).as_dict(),
        "climatology_vs_market": brier_difference(obs, "p_climo", n_boot).as_dict(),
        "brier": {
            c: float(brier(obs[c].to_numpy(float), y).mean()) for c in ("p_mkt", "p_nbm", "p_climo")
        },
        "logloss": {
            c: float(logloss(obs[c].to_numpy(float), y).mean())
            for c in ("p_mkt", "p_nbm", "p_climo")
        },
        "murphy": {c: murphy(obs[c].to_numpy(float), y) for c in ("p_mkt", "p_nbm", "p_climo")},
        "reliability": {
            c: reliability_table(obs[c].to_numpy(float), y) for c in ("p_mkt", "p_nbm", "p_climo")
        },
        "strata": {},
        "taker": {str(t): s for t, s in summaries.items()},
        "base_rate": float(y.mean()),
    }
    for col in ("kind", "lead_band", "version"):
        out["strata"][col] = {}
        for key, g in obs.groupby(col):
            yy = g["y"].to_numpy(float)
            d = brier(g["p_mkt"].to_numpy(float), yy) - brier(g["p_nbm"].to_numpy(float), yy)
            entry: dict[str, Any] = {
                "n": len(g),
                "weeks": int(g["week"].nunique()),
                "events": int(g["event_ticker"].nunique()),
                "D": float(d.mean()),
            }
            # Storm-window stratum: point estimate only (registered).
            if (col != "kind" or key != "storm") and g["week"].nunique() >= 5:
                lo, hi = np.quantile(
                    block_bootstrap_mean(d, g["week"].to_numpy(), n_boot=n_boot), [0.025, 0.975]
                )
                entry["ci"] = [float(lo), float(hi)]
            out["strata"][col][str(key)] = entry
    out["_obs"] = obs
    out["_trades"] = trades[PRIMARY_TAU]
    return out


def synthetic_world(
    n_weeks: int = 60,
    per_week: int = 120,
    nbm_noise: float = 0.0,
    mkt_noise: float = 0.0,
    seed: int = 1,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int], dict[str, datetime]]:
    """A world with known truth, in the same table shapes as the real inputs.

    True probability p* per market; outcome ~ Bernoulli(p*); each forecaster sees
    p* with logit noise of the given size; quotes are the noisy market belief
    +/- 0.02. Strongly clustered by week (a shared weekly shock), like storms.
    """
    rng = np.random.default_rng(seed)
    rows, qrows, outcomes, ends = [], [], {}, {}
    t0 = datetime(2024, 1, 1, 14, 30, tzinfo=UTC)

    def noisy(p: np.ndarray, s: float) -> np.ndarray:
        z = np.log(p / (1 - p)) + rng.normal(0, s, size=p.shape)
        return np.asarray(1 / (1 + np.exp(-z)))

    for w in range(n_weeks):
        shock = rng.normal(0, 1.0)
        for i in range(per_week):
            tk = f"SYN-{w}-{i}"
            ev = f"SYN-{w}-{i // 6}"
            z = rng.normal(-0.5 + shock, 1.5)
            p_true = float(1 / (1 + np.exp(-z)))
            y = int(rng.random() < p_true)
            t = t0 + timedelta(days=7 * w, hours=(i % 6) * 6)
            p_n = float(noisy(np.array([p_true]), nbm_noise)[0])
            p_m = float(np.clip(noisy(np.array([p_true]), mkt_noise)[0], 0.03, 0.97))
            rows.append(
                {
                    "event_ticker": ev, "series": "SYN", "kind": "monthly", "station": "KSYN",
                    "ticker": tk, "strike": 1.0, "t": t, "p_nbm": p_n, "p_nbm_indep": p_n,
                    "p_climo": 0.3, "determined": False, "nbm_versions": "V4.3",
                }
            )  # fmt: skip
            qrows.append(
                {
                    "ticker": tk, "candle_end": t.replace(minute=0), "bid": round(p_m - 0.02, 2),
                    "ask": round(p_m + 0.02, 2), "volume": 2000.0,
                }
            )  # fmt: skip
            outcomes[tk] = y
            ends[ev] = t + timedelta(days=5)
    f = pd.DataFrame(rows)
    f["t"] = pd.to_datetime(f["t"], utc=True)
    q = pd.DataFrame(qrows)
    q["candle_end"] = pd.to_datetime(q["candle_end"], utc=True)
    return f, q, outcomes, ends


# ---------- report ----------

SERIES_STYLE = {  # validated categorical slots 1-3 (dataviz reference palette, light mode)
    "p_mkt": ("Kalshi market", "#2a78d6", "o"),
    "p_nbm": ("NBM composite", "#eb6834", "s"),
    "p_climo": ("Climatology", "#1baf7a", "^"),
}


def reliability_figure(result: dict[str, Any], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ink, muted, surface = "#0b0b0b", "#52514e", "#fcfcfb"
    fig, ax = plt.subplots(figsize=(6.4, 6.0), dpi=150, facecolor=surface)
    ax.set_facecolor(surface)
    ax.plot([0, 1], [0, 1], color="#b9b8b2", lw=1, ls=(0, (4, 3)), zorder=1)
    for col, (label, color, marker) in SERIES_STYLE.items():
        rows = [r for r in result["reliability"][col] if r["n"] >= 20]
        xs = [r["mean_p"] for r in rows]
        ys = [r["freq"] for r in rows]
        ax.plot(
            xs,
            ys,
            color=color,
            lw=2,
            marker=marker,
            ms=6,
            mec=surface,
            mew=1.5,
            label=label,
            zorder=3,
        )
        if rows:
            ax.annotate(label, (xs[-1], ys[-1]), xytext=(6, -2), textcoords="offset points",
                        color=ink, fontsize=8.5, va="center")  # fmt: skip
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("Forecast probability (bin mean)", color=muted, fontsize=9)
    ax.set_ylabel("Observed frequency", color=muted, fontsize=9)
    ax.set_title(
        "Reliability, P2A observations (bins with 20+ observations)",
        color=ink,
        fontsize=10,
        loc="left",
    )
    ax.grid(color="#e6e5e0", lw=0.6)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#c9c8c2")
    ax.tick_params(colors=muted, labelsize=8)
    ax.legend(frameon=False, fontsize=8.5, loc="upper left", labelcolor=ink)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=surface)
    plt.close(fig)


def _fmt(x: float, nd: int = 4) -> str:
    return f"{x:+.{nd}f}" if isinstance(x, float) else str(x)


def report_markdown(r: dict[str, Any], figure_name: str) -> str:
    p = r["primary"]
    t10 = r["taker"][str(PRIMARY_TAU)]
    lines = [
        f"**Verdict under the registered rule: {r['verdict']}**",
        "",
        "## Primary metric",
        "",
        "D = Brier(market) - Brier(NBM composite); positive means NBM is more accurate.",
        "",
        "| | Observations | Weekly blocks | D | 95% CI (block bootstrap) |",
        "| - | -: | -: | -: | - |",
        f"| Primary (spread <= 0.20) | {p['n']:,} | {p['weeks']} | {_fmt(p['D'])} | [{_fmt(p['ci_low'])}, {_fmt(p['ci_high'])}] |",
    ]
    for s, d in r["sensitivity_spread"].items():
        lines.append(
            f"| Sensitivity: spread <= {s} | {d['n']:,} | {d['weeks']} | {_fmt(d['D'])} | [{_fmt(d['ci_low'])}, {_fmt(d['ci_high'])}] |"
        )
    d = r["nbm_independent_days"]
    lines.append(
        f"| Sensitivity: NBM with independent days | {d['n']:,} | {d['weeks']} | {_fmt(d['D'])} | [{_fmt(d['ci_low'])}, {_fmt(d['ci_high'])}] |"
    )
    d = r["climatology_vs_market"]
    lines.append(
        f"| Reference: climatology instead of NBM | {d['n']:,} | {d['weeks']} | {_fmt(d['D'])} | [{_fmt(d['ci_low'])}, {_fmt(d['ci_high'])}] |"
    )
    lines += [
        "",
        "## Scores",
        "",
        f"Base rate (share of observations that resolved Yes): {r['base_rate']:.3f}",
        "",
        "| Forecast | Brier | Log loss | Reliability | Resolution | Uncertainty |",
        "| - | -: | -: | -: | -: | -: |",
    ]
    for col, (label, _c, _m) in SERIES_STYLE.items():
        m = r["murphy"][col]
        lines.append(
            f"| {label} | {r['brier'][col]:.4f} | {r['logloss'][col]:.4f} | {m['reliability']:.4f} | {m['resolution']:.4f} | {m['uncertainty']:.4f} |"
        )
    lines += [
        "",
        f"![Reliability diagram]({figure_name})",
        "",
        "Reliability table (bins of forecast probability):",
        "",
    ]
    lines += [
        "| Bin | "
        + " | ".join(f"{SERIES_STYLE[c][0]} n / mean p / freq" for c in SERIES_STYLE)
        + " |",
        "| - | - | - | - |",
    ]
    tables = {c: {row["bin"]: row for row in r["reliability"][c]} for c in SERIES_STYLE}
    for b in [k / 10 for k in range(10)]:
        cells = []
        for c in SERIES_STYLE:
            row = tables[c].get(b)
            cells.append(f"{row['n']} / {row['mean_p']:.2f} / {row['freq']:.2f}" if row else "-")
        lines.append(f"| {b:.1f}-{b + 0.1:.1f} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Strata (point estimates of D; intervals where 5+ weekly blocks)",
        "",
        "| Stratum | Value | Observations | Weeks | Events | D | 95% CI |",
        "| - | - | -: | -: | -: | -: | - |",
    ]
    for col, groups in r["strata"].items():
        for key, g in groups.items():
            ci = f"[{_fmt(g['ci'][0])}, {_fmt(g['ci'][1])}]" if "ci" in g else "n/a"
            lines.append(
                f"| {col} | {key} | {g['n']:,} | {g['weeks']} | {g['events']} | {_fmt(g['D'])} | {ci} |"
            )
    lines += [
        "",
        "## Naive taker simulation (after fees, volume-capped)",
        "",
        "| tau | Trades | Contracts | P&L ($) | Fees ($) | P&L per contract | Resamples with P&L > 0 | 95% CI ($) | Max drawdown ($) | Weeks traded | Events traded | Corr(perceived, realized edge) |",
        "| -: | -: | -: | -: | -: | -: | -: | - | -: | -: | -: | -: |",
    ]
    for tau, s in r["taker"].items():
        if not s.get("trades"):
            lines.append(f"| {tau} | 0 | | | | | | | | | | |")
            continue
        lines.append(
            f"| {tau} | {s['trades']} | {s['contracts']:,} | {s['pnl']:,.0f} | {s['fees']:,.0f} | {s['pnl_per_contract']:+.3f} | "
            f"{s['share_resamples_positive']:.1%} | [{s['pnl_ci'][0]:,.0f}, {s['pnl_ci'][1]:,.0f}] | {s['max_drawdown']:,.0f} | "
            f"{s['weeks_with_trades']} | {s['events_traded']} | {s['perceived_vs_realized_corr']:.2f} |"
        )
    if t10.get("by_kind"):
        lines += [
            "",
            "P&L at tau = 0.10 by market type: "
            + ", ".join(f"{k} ${v:,.0f}" for k, v in t10["by_kind"].items()),
        ]
    return "\n".join(lines) + "\n"


def write_outputs(r: dict[str, Any], out_dir: Path, figure_path: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in r.items() if not k.startswith("_")}
    (out_dir / "result.json").write_text(json.dumps(clean, indent=1, default=str))
    r["_obs"].to_parquet(out_dir / "observations.parquet")
    if not r["_trades"].empty:
        r["_trades"].to_parquet(out_dir / "trades_tau010.parquet")
    reliability_figure(r, figure_path)
