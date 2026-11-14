"""Design analysis behind three choices in the P2A pre-registration.

Uses ONLY quotes, volumes and market structure of settled Kalshi snow markets,
plus simulation. It never loads a forecast and never scores a price against an
outcome, so it cannot leak the result of the registered comparison.

    uv run python scripts/prereg_design.py pull      # cache hourly candles (about 5 min)
    uv run python scripts/prereg_design.py analyze   # print the tables

Questions:
  1. Which maximum quoted spread should count as a usable market quote?
  2. Pool storm-window and monthly markets for the primary metric, or storm only?
  3. What bootstrap share of positive simulated P&L should a GO require?
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from pmw.markets.kalshi import KalshiPublic
from pmw.utils.timeutil import parse_ts

SERIES = [
    "KXNYCSNOWM", "KXBOSSNOWM", "KXDCSNOWM", "KXPHILSNOWM", "KXCHISNOWM", "KXDETSNOWM",
    "KXSEASNOWM", "KXSLCSNOWM", "KXDALSNOWM", "KXAUSSNOWM", "KXHOUSNOWM", "KXSNOWNYM",
    "KXSNOWSTORM",
]  # fmt: skip
EXCLUDED_EVENTS = {"KXCHISNOWM-25DEC", "KXCHISNOWM-25DECB"}  # pre-registered exclusion
DECISION_HOURS = {2, 8, 14, 20}  # last hourly candle closed before 02:30, 08:30, ... UTC
RAW = Path("data/raw/kalshi/prereg_design")
THRESHOLDS = [0.05, 0.10, 0.15, 0.20, 0.30, 0.50, 1.00]
RNG = np.random.default_rng(20261114)


def pull() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    client = KalshiPublic(min_request_interval_s=0.5)
    markets = []
    for series in SERIES:
        for m in client.settled_markets(series):
            markets.append({**m, "series_ticker": series})
    (RAW / "markets.json").write_text(json.dumps(markets))
    for i, m in enumerate(markets):
        path = RAW / f"candles_{m['ticker']}.json"
        if path.exists():
            continue
        start, end = parse_ts(m["open_time"]), parse_ts(m["close_time"])
        assert start is not None and end is not None
        candles = client.historical_candlesticks(
            m["ticker"], start_ts=int(start.timestamp()), end_ts=int(end.timestamp())
        )
        path.write_text(json.dumps(candles))
        if i % 50 == 0:
            print(f"  {i}/{len(markets)} markets", flush=True)
    print(f"cached {len(markets)} markets, {client.request_count} requests")


def _px(node: dict | None, key: str) -> float | None:
    if not node:
        return None
    value = node.get(key, node.get(f"{key}_dollars"))
    return None if value in (None, "") else float(value)


def load() -> pd.DataFrame:
    rows = []
    for m in json.loads((RAW / "markets.json").read_text()):
        if m["event_ticker"] in EXCLUDED_EVENTS or m.get("result") not in ("yes", "no"):
            continue
        kind = "storm" if m["series_ticker"] == "KXSNOWSTORM" else "monthly"
        for c in json.loads((RAW / f"candles_{m['ticker']}.json").read_text()):
            ts = datetime.fromtimestamp(c["end_period_ts"], UTC)
            ref = _px(c.get("price"), "close") or _px(c.get("price"), "previous")
            rows.append(
                {
                    "ticker": m["ticker"],
                    "event": m["event_ticker"],
                    "kind": kind,
                    "ts": ts,
                    "week": f"{ts.isocalendar().year}-W{ts.isocalendar().week:02d}",
                    "bid": _px(c.get("yes_bid"), "close"),
                    "ask": _px(c.get("yes_ask"), "close"),
                    "ref": ref,
                    "volume": float(c.get("volume", c.get("volume_fp")) or 0),
                }
            )
    df = pd.DataFrame(rows)
    df["two_sided"] = (df.bid >= 0.01) & (df.ask <= 0.99) & (df.ask > df.bid)
    df["spread"] = np.where(df.two_sided, df.ask - df.bid, np.nan)
    # "Uncertain": the last trade says the outcome is still open. Quote-free on
    # purpose, so the spread filter is judged on hours that matter.
    df["uncertain"] = df.ref.between(0.05, 0.95)
    df["decision"] = df.ts.dt.hour.isin(DECISION_HOURS)
    return df


def kish(weights: np.ndarray) -> float:
    w = np.asarray(weights, dtype=float)
    return float(w.sum() ** 2 / (w**2).sum()) if w.sum() > 0 else 0.0


def spread_tables(df: pd.DataFrame) -> pd.DataFrame:
    base = df[df.decision & df.uncertain]
    print(f"\n[1] SPREAD FILTER. Decision-time, still-uncertain observations: {len(base):,}")
    print(base.groupby("kind").agg(obs=("ticker", "size"), events=("event", "nunique"),
                                   weeks=("week", "nunique"), two_sided=("two_sided", "mean"),
                                   median_spread=("spread", "median")).round(3))  # fmt: skip
    out = []
    for thr in THRESHOLDS:
        keep = base[base.two_sided & (base.spread <= thr + 1e-9)]
        row = {"max_spread": thr, "kept_all": len(keep) / len(base)}
        for kind in ("monthly", "storm"):
            b, k = base[base.kind == kind], keep[keep.kind == kind]
            row[f"kept_{kind}"] = len(k) / len(b)
            row[f"events_{kind}"] = f"{k.event.nunique()}/{b.event.nunique()}"
        row["weeks"] = f"{keep.week.nunique()}/{base.week.nunique()}"
        row["n_eff_weeks"] = round(kish(keep.groupby("week").size().to_numpy()), 1)
        # Worst-case Brier inflation from scoring the mid when the market's true
        # belief is uniform inside [bid, ask]: E[(p - mid)^2] = s^2 / 12.
        row["mid_noise_brier"] = round(float((keep.spread**2).mean() / 12), 4)
        out.append(row)
    table = pd.DataFrame(out)
    print(table.round(3).to_string(index=False))
    two = base[base.two_sided].copy()
    two["bucket"] = pd.cut(two.spread, [0, 0.05, 0.10, 0.20, 0.30, 1.0])
    print("\nAre wide-spread hours the quiet ones or the active ones?")
    print(two.groupby(["kind", "bucket"], observed=True).agg(
        obs=("ticker", "size"), mean_hourly_volume=("volume", "mean"),
        share_with_trades=("volume", lambda v: (v > 0).mean())).round(2))  # fmt: skip
    return table


def block_tables(df: pd.DataFrame, thr: float) -> dict[str, np.ndarray]:
    obs = df[df.decision & df.uncertain & df.two_sided & (df.spread <= thr + 1e-9)]
    print(f"\n[2] BLOCK STRUCTURE at max spread {thr:.2f} (block = calendar week, all cities)")
    weights = {}
    for name, sub, volsrc in (
        ("pooled", obs, df),
        ("monthly only", obs[obs.kind == "monthly"], df[df.kind == "monthly"]),
        ("storm only", obs[obs.kind == "storm"], df[df.kind == "storm"]),
    ):
        n_week = sub.groupby("week").size()
        vol_week = volsrc.groupby("week").volume.sum().reindex(n_week.index).fillna(0)
        weights[name] = n_week.to_numpy()
        weights[name + " (volume)"] = vol_week.to_numpy()
        print(f"  {name:<13} obs={len(sub):>6,}  weeks={len(n_week):>3}  "
              f"effective weeks: obs-weighted={kish(n_week.to_numpy()):5.1f}  "
              f"volume-weighted={kish(vol_week.to_numpy()):5.1f}  "
              f"largest week share: obs={n_week.max() / n_week.sum():.0%} "
              f"volume={vol_week.max() / max(vol_week.sum(), 1):.0%}")  # fmt: skip
    storm = df[df.kind == "storm"]
    print("\n  Storm events:")
    print(storm.groupby("event").agg(first=("ts", "min"), last=("ts", "max"),
                                     markets=("ticker", "nunique"), contracts=("volume", "sum"),
                                     weeks=("week", lambda w: ",".join(sorted(set(w))))))  # fmt: skip
    share = df.groupby("kind").volume.sum() / df.volume.sum()
    n_share = obs.groupby("kind").size() / len(obs)
    print(f"\n  Share of contracts traded: storm {share.get('storm', 0):.0%}, "
          f"monthly {share.get('monthly', 0):.0%}. Share of pooled observations: "
          f"storm {n_share.get('storm', 0):.1%}, monthly {n_share.get('monthly', 0):.1%}")  # fmt: skip
    return weights


def _boot_stats(w: np.ndarray, x: np.ndarray, n_boot: int) -> tuple[np.ndarray, np.ndarray]:
    """Weighted block means under block resampling. x has shape (sims, blocks)."""
    idx = RNG.integers(0, len(w), size=(n_boot, len(w)))
    wb = w[idx]  # (boot, blocks)
    means = (x[:, idx] * wb).sum(axis=2) / wb.sum(axis=1)  # (sims, boot)
    return means, (x * w).sum(axis=1) / w.sum()


def coverage_table(weights: dict[str, np.ndarray], sims: int = 1500, n_boot: int = 800) -> None:
    print("\n[2b] Does a 95% block-bootstrap interval hold its level with this many blocks?")
    print("     True effect is zero; block effects are heavy-tailed (Student t, 4 df).")
    print("     'false signal' = interval excludes zero. Nominal is 5.0%.")
    for name in ("pooled", "monthly only", "storm only"):
        w = weights[name].astype(float)
        if len(w) < 2:
            print(f"  {name:<13} too few blocks to bootstrap ({len(w)})")
            continue
        x = RNG.standard_t(4, size=(sims, len(w))) / np.sqrt(np.maximum(w / w.mean(), 0.05))
        means, _ = _boot_stats(w, x, n_boot)
        lo, hi = np.quantile(means, [0.025, 0.975], axis=1)
        print(f"  {name:<13} blocks={len(w):>3}  false signal rate = "
              f"{((lo > 0) | (hi < 0)).mean():.1%}")  # fmt: skip


def go_threshold_table(weights: dict[str, np.ndarray], sims: int = 1500, n_boot: int = 800) -> None:
    print("\n[3] GO THRESHOLD. GO needs BOTH: Brier-difference 95% CI above zero AND")
    print("    simulated P&L positive in at least T of bootstrap resamples.")
    print("    Brier uses observation weights; P&L uses volume weights (money is concentrated).")
    w_d = weights["pooled"].astype(float)
    w_p = np.maximum(weights["pooled (volume)"].astype(float), 1.0)
    rho = 0.6  # correlation between a week's Brier gain and its P&L
    idx = RNG.integers(0, len(w_d), size=(n_boot, len(w_d)))
    thresholds = [0.50, 0.75, 0.90, 0.95, 0.975]

    def run(effect_d: float, effect_p: float) -> list[float]:
        z1 = RNG.standard_t(4, size=(sims, len(w_d)))
        z2 = rho * z1 + np.sqrt(1 - rho**2) * RNG.standard_t(4, size=(sims, len(w_d)))
        # Noise per block shrinks with block size; effects are in units of the
        # standard error of the weighted mean, so "2.0" means an expected t of 2.
        nd = z1 / np.sqrt(np.maximum(w_d / w_d.mean(), 0.05))
        np_ = z2 / np.sqrt(np.maximum(w_p / w_p.mean(), 0.05))
        se_d = ((nd * w_d).sum(axis=1) / w_d.sum()).std()
        se_p = ((np_ * w_p).sum(axis=1) / w_p.sum()).std()
        xd, xp = nd + effect_d * se_d, np_ + effect_p * se_p
        md = (xd[:, idx] * w_d[idx]).sum(axis=2) / w_d[idx].sum(axis=1)
        mp = (xp[:, idx] * w_p[idx]).sum(axis=2) / w_p[idx].sum(axis=1)
        brier_ok = np.quantile(md, 0.025, axis=1) > 0
        share_pos = (mp > 0).mean(axis=1)
        return [float(brier_ok.mean())] + [
            float((brier_ok & (share_pos >= t)).mean()) for t in thresholds
        ]

    rows = []
    for label, ed, ep in (
        ("no edge at all", 0.0, 0.0),
        ("NBM better, fees eat it (P&L = 0)", 2.5, 0.0),
        ("NBM better, P&L negative", 2.5, -1.0),
        ("modest real edge (t about 2)", 2.0, 2.0),
        ("clear real edge (t about 3)", 3.0, 3.0),
        ("P&L edge weaker than Brier edge", 3.0, 1.5),
    ):
        rows.append([label, *run(ed, ep)])
    table = pd.DataFrame(
        rows, columns=["scenario", "Brier CI only", *[f"T={t}" for t in thresholds]]
    )
    print(table.to_string(index=False, float_format=lambda v: f"{v:6.1%}"))
    print(f"    (pooled: {len(w_d)} weekly blocks; effective {kish(w_d):.1f} by observations, "
          f"{kish(w_p):.1f} by volume)")  # fmt: skip


def analyze() -> None:
    df = load()
    print(f"markets: {df.ticker.nunique()}  events: {df.event.nunique()}  market-hours: {len(df):,}  "
          f"span: {df.ts.min():%Y-%m-%d} to {df.ts.max():%Y-%m-%d}")  # fmt: skip
    spread_tables(df)
    weights = block_tables(df, 0.20)
    for thr in (0.10, 0.30):
        obs = df[df.decision & df.uncertain & df.two_sided & (df.spread <= thr + 1e-9)]
        print(f"  (at max spread {thr:.2f}: pooled obs={len(obs):,}, weeks={obs.week.nunique()}, "
              f"effective weeks={kish(obs.groupby('week').size().to_numpy()):.1f})")  # fmt: skip
    coverage_table(weights)
    go_threshold_table(weights)


if __name__ == "__main__":
    {"pull": pull, "analyze": analyze}[sys.argv[1] if len(sys.argv) > 1 else "analyze"]()
