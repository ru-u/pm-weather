"""Experiment P2A-S: the P2A comparison on live-logged storm-window books.

Registered with P2A in docs/preregistration.md ("Prospective extension"): the
same comparison and decision rule as P2A, on storm-window markets logged live,
with quotes from the logger's order books instead of hourly candles. Scored
once, after the gate in `guard`; until then only `health` (counts) runs on the
sample. Everything P2A fixed is reused unchanged: the composite
(`p2a.build_forecasts`), the observation set (`p2a_score.assemble`), the Brier
difference with weekly blocks, the taker summary and the decision rule.

What is new is only what the change of quote source requires:
  1. sample:  storm-window markets from the logger's market metadata, one
              `EventSpec` per (event, station) because a KXSNOWCITIES event
              spans several cities; exclusions carry a reason instead of raising;
  2. quotes:  the latest logged book at or before each decision time (within
              QUOTE_WITHIN), plus the contracts traded in the hour P2A's candle
              covered, shaped as P2A's quote table so `assemble` applies as is;
  3. taker:   P2A's sizing rule (10% of that hour's volume, 250 per market and
              1,000 per event per UTC day, largest edge first), filled by walking
              the logged book with `pmw.backtest.taker.walk`.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pmw.backtest.taker import walk
from pmw.eval.p2a import EventSpec, MarketSpec
from pmw.eval.p2a_score import (
    EVENT_DAY_CAP,
    MARKET_DAY_CAP,
    MAX_SPREAD,
    N_BOOT,
    VOLUME_SHARE,
    score_all,
)
from pmw.utils.config import StationsConfig
from pmw.utils.timeutil import climate_day_bounds_utc

SERIES = ("KXSNOWSTORM", "KXSNOWCITIES")
STORM_GROUP = "snow_storm"
# A market enters the sample only if the logger recorded it from its open.
LOGGER_START = datetime(2026, 11, 14, 16, 12, tzinfo=UTC)
QUOTE_WITHIN = timedelta(minutes=10)
MIN_EVENTS, MIN_WEEKS = 12, 8
EVAL_NOT_BEFORE = date(2027, 5, 15)
INPUT_ROOT = Path("data/interim/live_snow")


# ---------- 1. sample ----------


@dataclass(frozen=True)
class Excluded:
    ticker: str
    event_ticker: str
    reason: str


def latest_meta(meta: pd.DataFrame) -> pd.DataFrame:
    """The latest metadata row per storm-window ticker."""
    m = meta[(meta["grp"] == STORM_GROUP) | meta["series_ticker"].isin(SERIES)]
    m = m[m["series_ticker"].isin(SERIES)]
    m = m.sort_values("received_at_utc").groupby("ticker", as_index=False).last()
    return m.reset_index(drop=True)


def _clean(r: dict[str, Any]) -> dict[str, Any]:
    return {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in r.items()}


def _utc(v: Any) -> datetime:
    return pd.Timestamp(v).tz_convert("UTC").to_pydatetime()


def sample_from_meta(
    meta: pd.DataFrame, stations: StationsConfig, dev: bool = False
) -> tuple[list[EventSpec], list[Excluded]]:
    """Registered sample (or, with dev, only markets that opened before the logger).

    Rules in order: station named in the rules and configured; a "greater" strike;
    a window read from the rule text; opened at or after LOGGER_START. Settlement
    replication is checked at scoring time, as in P2A.
    """
    from pmw.settle.snow import find_station, parse_period, parse_strike

    groups: dict[tuple[str, str], list[tuple[dict[str, Any], MarketSpec, list[date]]]] = (
        defaultdict(list)
    )
    excluded: list[Excluded] = []
    stations_by_icao = {s.icao: s for s in stations.stations.values()}
    for raw in latest_meta(meta).to_dict("records"):
        r = _clean(raw)
        tk, ev = r["ticker"], r["event_ticker"]
        st = find_station(r, r["series_ticker"], stations)
        try:
            strike_type, floor, _cap = parse_strike(r)
        except ValueError:
            strike_type, floor = "", None
        try:
            window = parse_period(r.get("rules_primary") or "", ev)
        except ValueError:
            window = []
        opened = _utc(r["open_time"])
        if st is None:
            reason = "station_not_configured"
        elif strike_type != "greater" or floor is None:
            reason = "not_a_greater_strike"
        elif not window:
            reason = "window_unreadable"
        elif (opened < LOGGER_START) != dev:
            reason = "opened_before_logger" if not dev else "in_sample"
        else:
            reason = ""
        if reason:
            excluded.append(Excluded(tk, ev, reason))
            continue
        assert st is not None and floor is not None
        spec = MarketSpec(tk, strike_type, float(floor), opened, _utc(r["close_time"]))
        groups[(ev, st.icao)].append((r, spec, window))
    events = []
    for (ev, icao), items in sorted(groups.items()):
        windows = {tuple(w) for _, _, w in items}
        if len(windows) > 1:
            excluded += [Excluded(s.ticker, ev, "window_differs_within_event") for _, s, _ in items]
            continue
        events.append(EventSpec(
            event_ticker=ev, series=items[0][0]["series_ticker"], kind="storm",
            station=stations_by_icao[icao], window=list(items[0][2]),
            markets=sorted((s for _, s, _ in items), key=lambda s: s.strike),
        ))  # fmt: skip
    return events, excluded


def window_ends(events: list[EventSpec]) -> dict[str, datetime]:
    """Latest window end per event ticker (one value per KXSNOWCITIES event)."""
    out: dict[str, datetime] = {}
    for ev in events:
        end = climate_day_bounds_utc(ev.window[-1], ev.station.std_utc_offset_hours)[1]
        out[ev.event_ticker] = max(end, out.get(ev.event_ticker, end))
    return out


# ---------- 2. quotes ----------


def hour_volume(trades: pd.DataFrame, at: pd.Series, tickers: pd.Series) -> np.ndarray:
    """Contracts traded per ticker in the hour P2A's candle covered: [floor(t) - 1 h, floor(t))."""
    if trades.empty:
        return np.zeros(len(at))
    tr = trades.assign(ts=pd.to_datetime(trades["exchange_ts"], utc=True))
    tr["hour_end"] = tr["ts"].dt.floor("h") + pd.Timedelta(hours=1)
    vol = tr.groupby(["ticker", "hour_end"])["count"].sum()
    keys = pd.MultiIndex.from_arrays(
        [tickers.to_numpy(), pd.to_datetime(at, utc=True).dt.floor("h")]
    )
    return np.asarray(vol.reindex(keys).fillna(0.0).to_numpy(float))


def quotes_from_books(
    forecasts: pd.DataFrame, books: pd.DataFrame, trades: pd.DataFrame
) -> pd.DataFrame:
    """P2A's quote table built from logged books: one row per (ticker, decision hour).

    The book is the latest snapshot received at or before t and no older than
    QUOTE_WITHIN; no such snapshot means no observation, as a missing candle did.
    """
    cols = ["ticker", "candle_end", "bid", "ask", "volume", "quote_at",
            "yes_px", "yes_qty", "no_px", "no_qty"]  # fmt: skip
    want = forecasts[["ticker", "t"]].drop_duplicates()
    if want.empty or books.empty:
        return pd.DataFrame(columns=cols)
    b = books.assign(at=pd.to_datetime(books["received_at_utc"], utc=True))
    rows = []
    for tk, g in b.sort_values("at").groupby("ticker"):
        ts = g["at"].dt.tz_localize(None).to_numpy()
        for t in want.loc[want["ticker"] == tk, "t"]:
            t64 = pd.Timestamp(t).tz_convert("UTC").tz_localize(None).to_datetime64()
            i = int(np.searchsorted(ts, t64, side="right")) - 1
            if i < 0 or t64 - ts[i] > np.timedelta64(QUOTE_WITHIN):
                continue
            s = g.iloc[i]
            rows.append({"ticker": tk, "t": pd.Timestamp(t), "bid": s["best_yes_bid"],
                         "ask": s["best_yes_ask"], "quote_at": s["at"],
                         "yes_px": list(s["yes_px"]), "yes_qty": list(s["yes_qty"]),
                         "no_px": list(s["no_px"]), "no_qty": list(s["no_qty"])})  # fmt: skip
    q = pd.DataFrame(rows)
    if q.empty:
        return pd.DataFrame(columns=cols)
    q["t"] = pd.to_datetime(q["t"], utc=True)
    q["candle_end"] = q["t"].dt.floor("h")
    q["volume"] = hour_volume(trades, q["t"], q["ticker"])
    return q[cols]


# ---------- 3. taker on the book ----------


def simulate_taker_books(obs: pd.DataFrame, tau: float, prob_col: str = "p_nbm") -> pd.DataFrame:
    """P2A's naive taker (clarification 10) with fills from the logged book.

    Size cap: floor(min(10% of the hour's volume, 250)) per market per UTC day and
    what is left of 1,000 per event per UTC day. Within an (event, t), the largest
    perceived edge at the best level goes first. The book is walked level by level
    while the after-fee edge stays above tau. Output columns match P2A's
    `simulate_taker` (price and fee are per-contract averages), so
    `p2a_score.taker_summary` applies.
    """
    trades = []
    held: set[tuple[str, Any]] = set()
    used: dict[tuple[str, Any], float] = {}
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
            for side, q, opp_px, opp_qty in (("yes", p, r.no_px, r.no_qty),
                                             ("no", 1 - p, r.yes_px, r.yes_qty)):  # fmt: skip
                first = walk(q, list(opp_px), list(opp_qty), tau, 1.0)
                if first:
                    edge = q - first[0].price - first[0].fee / first[0].contracts
                    cands.append((edge, r, side, q, size))
                    break
        for _edge, r, side, q, size in sorted(cands, key=lambda c: -c[0]):
            key = (r.event_ticker, r.t.date())
            room = math.floor(min(size, EVENT_DAY_CAP - used.get(key, 0.0)))
            if room < 1:
                continue
            opp_px, opp_qty = (r.no_px, r.no_qty) if side == "yes" else (r.yes_px, r.yes_qty)
            fills = walk(q, list(opp_px), list(opp_qty), tau, room)
            n = sum(f.contracts for f in fills)
            if n <= 0:
                continue
            price = sum(f.price * f.contracts for f in fills) / n
            fee = sum(f.fee for f in fills) / n
            win = r.y if side == "yes" else 1 - r.y
            trades.append({
                "t": r.t, "week": r.week, "ticker": r.ticker, "event_ticker": r.event_ticker,
                "kind": r.kind, "side": side, "price": price, "contracts": n, "fee_per": fee,
                "perceived_edge": q - price, "realized_edge": win - price,
                "pnl": n * (win - price - fee), "levels": len(fills),
            })  # fmt: skip
            used[key] = used.get(key, 0.0) + n
            held.add((r.ticker, r.t.date()))
    return pd.DataFrame(trades)


# ---------- 4. gate, score, health ----------


def settled_coverage(events: list[EventSpec], outcomes: dict[str, int]) -> dict[str, int]:
    """Events with at least one settled, reproduced market, and their distinct ISO weeks."""
    starts: dict[str, date] = {}
    for ev in events:
        if any(m.ticker in outcomes for m in ev.markets):
            starts[ev.event_ticker] = min(ev.window[0], starts.get(ev.event_ticker, ev.window[0]))
    weeks = {d.isocalendar()[:2] for d in starts.values()}
    return {"events": len(starts), "weeks": len(weeks)}


def guard(coverage: dict[str, int], dev: bool, today: date | None = None) -> None:
    """Scored once: on or after EVAL_NOT_BEFORE, with at least 12 events in 8 weeks."""
    if dev:
        return
    today = today or datetime.now(UTC).date()
    if today < EVAL_NOT_BEFORE:
        raise RuntimeError(
            f"P2A-S is scored once on or after {EVAL_NOT_BEFORE}; health only until then"
        )
    if coverage["events"] < MIN_EVENTS or coverage["weeks"] < MIN_WEEKS:
        raise RuntimeError(
            f"P2A-S needs {MIN_EVENTS} settled storm events in {MIN_WEEKS} calendar weeks; "
            f"have {coverage['events']} in {coverage['weeks']}. Carry the sample forward."
        )


def score(
    forecasts: pd.DataFrame,
    quotes: pd.DataFrame,
    outcomes: dict[str, int],
    window_end: dict[str, datetime],
    n_boot: int = N_BOOT,
) -> dict[str, Any]:
    """P2A's `score_all`, with the book-walk taker in place of the candle taker.

    With no usable observation there is nothing to score: the counts say why.
    """
    counts = quote_counts(forecasts, quotes, outcomes)
    if counts["observations"] == 0:
        return {"verdict": None, "quote_counts": counts}
    out = score_all(forecasts, quotes, outcomes, window_end, n_boot=n_boot,
                    simulate=simulate_taker_books)  # fmt: skip
    out["quote_counts"] = counts
    return out


def quote_counts(
    forecasts: pd.DataFrame, quotes: pd.DataFrame, outcomes: dict[str, int]
) -> dict[str, int]:
    """Where forecast rows drop out on the way to P2A's observation set."""
    f = forecasts[~forecasts["determined"]]
    f = f.assign(candle_end=f["t"].dt.floor("h"))
    m = f.merge(quotes, on=["ticker", "candle_end"], how="inner")
    two = (m["bid"] >= 0.01) & (m["ask"] <= 0.99) & (m["ask"] > m["bid"])
    narrow = two & ((m["ask"] - m["bid"]) <= MAX_SPREAD + 1e-9)
    settled = narrow & m["ticker"].isin(set(outcomes))
    return {"forecast_rows": len(forecasts), "undetermined": len(f), "with_book": len(m),
            "two_sided": int(two.sum()), "spread_ok": int(narrow.sum()),
            "observations": int(settled.sum())}  # fmt: skip


def outcomes_from_checks(checks: list[Any]) -> tuple[dict[str, int], list[str]]:
    """Kalshi results for reproduced markets, and the settled tickers we do not reproduce."""
    out, bad = {}, []
    for c in checks:
        if c.kalshi_result in ("yes", "no") and c.match:
            out[c.ticker] = int(c.kalshi_result == "yes")
        elif c.kalshi_result in ("yes", "no"):
            bad.append(c.ticker)
    return out, bad


def health(
    events: list[EventSpec], excluded: list[Excluded], books: pd.DataFrame
) -> dict[str, Any]:
    """Counts only: no price is joined to a forecast or an outcome."""
    tickers = {m.ticker for ev in events for m in ev.markets}
    snaps: dict[str, Any] = {}
    if not books.empty:
        b = books[books["ticker"].isin(tickers)]
        at = pd.to_datetime(b["received_at_utc"], utc=True)
        for tk, g in at.groupby(b["ticker"]):
            snaps[str(tk)] = {"snapshots": len(g), "first": str(g.min()), "last": str(g.max())}
    reasons: dict[str, int] = defaultdict(int)
    for x in excluded:
        reasons[x.reason] += 1
    return {
        "events": len({ev.event_ticker for ev in events}),
        "markets": len(tickers),
        "excluded_by_reason": dict(reasons),
        "excluded": {x.ticker: x.reason for x in excluded},
        "markets_without_books": sorted(tickers - set(snaps)),
        "books": snaps,
        "gate": {
            "min_events": MIN_EVENTS,
            "min_weeks": MIN_WEEKS,
            "not_before": str(EVAL_NOT_BEFORE),
        },
    }


def _levels(side: str, best: float, depth: float) -> dict[str, list[float]]:
    px = [round(best - k / 100, 4) for k in (2, 1, 0) if best - k / 100 >= 0.01 - 1e-9]
    return {f"{side}_px": px, f"{side}_qty": [depth] * len(px)}


def synthetic_books(
    quotes: pd.DataFrame, depth: float = 2000.0
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Books and trades equivalent to P2A's synthetic candles, for known-answer runs.

    Each candle (bid, ask, volume) becomes a book snapshot five minutes before the
    decision time with three levels per side (best at the candle's bid and ask),
    and one trade of the candle's volume inside the candle's hour.
    """
    books, trades = [], []
    for r in quotes.itertuples(index=False):
        t = pd.Timestamp(r.candle_end) + pd.Timedelta(minutes=25)
        no_best = round(1 - r.ask, 4)
        books.append({
            "ticker": r.ticker, "received_at_utc": t, "best_yes_bid": r.bid, "best_yes_ask": r.ask,
            **_levels("yes", r.bid, depth), **_levels("no", no_best, depth),
        })  # fmt: skip
        traded_at = pd.Timestamp(r.candle_end) - pd.Timedelta(minutes=30)
        trades.append({"ticker": r.ticker, "count": r.volume, "exchange_ts": traded_at})
    return pd.DataFrame(books), pd.DataFrame(trades)
