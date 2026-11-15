"""Experiment P2A (pre-registered in docs/preregistration.md).

Stage 1, `build_forecasts`: for every market in the registered sample and every
registered decision time while it was open, the NBM composite probability (and
its sensitivity variant) and the climatology probability. Reads forecasts,
observations and climatology only. No prices, no outcomes.

Stage 2, `score` (in pmw.eval.p2a_score): joins market quotes and outcomes.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from pmw.models.nbm_composite import (
    Climatology,
    Composite,
    NbpForecast,
    NbpIndex,
    NbsForecast,
    NbsIndex,
    ObservedDay,
    build_composite,
)
from pmw.settle.snow import find_station, parse_period, parse_strike
from pmw.utils.config import Station, StationsConfig
from pmw.utils.timeutil import parse_ts

log = logging.getLogger(__name__)

REGISTERED_SERIES = (
    "KXNYCSNOWM", "KXBOSSNOWM", "KXDCSNOWM", "KXPHILSNOWM", "KXCHISNOWM", "KXDETSNOWM",
    "KXSEASNOWM", "KXSLCSNOWM", "KXDALSNOWM", "KXAUSSNOWM", "KXHOUSNOWM", "KXSNOWNYM",
    "KXSNOWSTORM",
)  # fmt: skip
EXCLUDED_EVENTS = frozenset({"KXCHISNOWM-25DEC", "KXCHISNOWM-25DECB"})
DECISION_TIMES_UTC = (time(2, 30), time(8, 30), time(14, 30), time(20, 30))


@dataclass
class MarketSpec:
    ticker: str
    strike_type: str
    strike: float
    open_time: datetime
    close_time: datetime


@dataclass
class EventSpec:
    event_ticker: str
    series: str
    kind: str  # "storm" | "monthly"
    station: Station
    window: list[date]
    markets: list[MarketSpec]


def load_sample(markets_path: Path, stations: StationsConfig) -> list[EventSpec]:
    """Registered sample: settled yes/no markets of the registered series, minus the
    pre-registered exclusions. Settlement replication is checked separately."""
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in json.loads(markets_path.read_text()):
        if m["series_ticker"] not in REGISTERED_SERIES or m["event_ticker"] in EXCLUDED_EVENTS:
            continue
        if m.get("result") not in ("yes", "no"):
            continue
        by_event[m["event_ticker"]].append(m)
    events = []
    for event_ticker, ms in sorted(by_event.items()):
        series = ms[0]["series_ticker"]
        station = find_station(ms[0], series, stations)
        if station is None:
            raise ValueError(f"no station for {event_ticker}")
        window = parse_period(ms[0].get("rules_primary") or "", event_ticker)
        specs = []
        for m in ms:
            strike_type, floor, _cap = parse_strike(m)
            if strike_type != "greater" or floor is None:
                raise ValueError(f"unexpected strike on {m['ticker']}")
            o, c = parse_ts(m["open_time"]), parse_ts(m["close_time"])
            assert o is not None and c is not None
            specs.append(MarketSpec(m["ticker"], strike_type, float(floor), o, c))
        events.append(
            EventSpec(
                event_ticker=event_ticker,
                series=series,
                kind="storm" if series == "KXSNOWSTORM" else "monthly",
                station=station,
                window=window,
                markets=sorted(specs, key=lambda s: s.strike),
            )
        )
    return events


def decision_times(start: datetime, end: datetime) -> list[datetime]:
    """Registered decision times t with start <= t < end."""
    out = []
    day = start.date()
    while datetime.combine(day, time(0), tzinfo=UTC) < end:
        for tt in DECISION_TIMES_UTC:
            t = datetime.combine(day, tt, tzinfo=UTC)
            if start <= t < end:
                out.append(t)
        day += timedelta(days=1)
    return out


# ---------- inputs per station ----------


def load_nbp(path: Path, station: str) -> NbpIndex:
    idx = NbpIndex()
    cols = ["station", "cycle", "available_at", "nbm_version", "valid_end"]
    pct_cols = [f"snow_p{p}" for p in (10, 25, 50, 75, 90)]
    table = pq.read_table(path, columns=cols + pct_cols, filters=[("station", "=", station)])
    for r in table.to_pylist():
        pct = tuple(float("nan") if r[c] is None else float(r[c]) for c in pct_cols)
        idx.add(
            r["valid_end"],
            NbpForecast(r["cycle"], r["available_at"], r["nbm_version"], pct),  # type: ignore[arg-type]
        )
    return idx.freeze()


def load_nbs(path: Path | None, station: str) -> NbsIndex | None:
    if path is None or not path.exists():
        return None
    table = pq.read_table(path, filters=[("station", "=", station)])
    by_cycle: dict[datetime, tuple[datetime, dict[datetime, float | None]]] = {}
    for r in table.to_pylist():
        entry = by_cycle.setdefault(r["cycle"], (r["available_at"], {}))
        entry[1][r["valid_end"]] = None if r["s06_in"] is None else float(r["s06_in"])
    return NbsIndex([NbsForecast(c, a, s06) for c, (a, s06) in by_cycle.items()])


def load_observed(path: Path, station: str) -> dict[date, ObservedDay]:
    table = pq.read_table(path, filters=[("station", "=", station)])
    return {
        r["date"]: ObservedDay(
            None if r["snow_in"] is None else float(r["snow_in"]), bool(r["trace"])
        )
        for r in table.to_pylist()
    }


def load_climatology(path: Path, station: str) -> Climatology:
    table = pq.read_table(path, filters=[("station", "=", station)])
    return Climatology(
        {r["date"]: (None if r["missing"] else float(r["snow_in"])) for r in table.to_pylist()}
    )


# ---------- stage 1 ----------

FORECAST_SCHEMA = pa.schema(
    [
        ("event_ticker", pa.string()),
        ("series", pa.string()),
        ("kind", pa.string()),
        ("station", pa.string()),
        ("ticker", pa.string()),
        ("strike", pa.float64()),
        ("t", pa.timestamp("us", tz="UTC")),
        ("p_nbm", pa.float64()),  # primary: Spearman 0.5 between forecast periods
        ("p_nbm_indep", pa.float64()),  # registered sensitivity: independent periods
        ("p_climo", pa.float64()),
        ("determined", pa.bool_()),  # outcome already fixed by observed snow at t
        ("observed_in", pa.float64()),
        ("n_observed_days", pa.int16()),
        ("n_forecast_periods", pa.int16()),
        ("forecast_days_equiv", pa.float64()),  # sum of forecast weights
        ("climo_days_equiv", pa.float64()),
        ("nbm_versions", pa.string()),
        ("weight_sources", pa.string()),
        ("max_forecast_available_at", pa.timestamp("us", tz="UTC")),
        ("max_observed_public_at", pa.timestamp("us", tz="UTC")),
    ]
)


def _audit(c: Composite) -> dict[str, Any]:
    return {
        "observed_in": c.observed_in,
        "n_observed_days": len(c.observed_days),
        "n_forecast_periods": len(c.forecast),
        "forecast_days_equiv": float(sum(p.weight for p in c.forecast)),
        "climo_days_equiv": float(sum(p.weight for p in c.climo)),
        "nbm_versions": ",".join(sorted({p.version for p in c.forecast})),
        "weight_sources": ",".join(sorted({p.weight_source for p in c.forecast})),
        "max_forecast_available_at": max((p.available_at for p in c.forecast), default=None),
        "max_observed_public_at": max(c.observed_public_at, default=None),
    }


def build_forecasts(
    events: list[EventSpec],
    *,
    nbp_path: Path,
    nbs_path: Path | None,
    obs_path: Path,
    climo_path: Path,
    out: Path,
) -> dict[str, int]:
    rows: list[dict[str, Any]] = []
    cache: dict[str, tuple[NbpIndex, NbsIndex | None, dict[date, ObservedDay], Climatology]] = {}
    n_times = 0
    for ev in events:
        sid = ev.station.icao
        if sid not in cache:
            cache[sid] = (
                load_nbp(nbp_path, sid),
                load_nbs(nbs_path, sid),
                load_observed(obs_path, sid),
                load_climatology(climo_path, sid),
            )
        nbp, nbs, observed, clim = cache[sid]
        start = min(m.open_time for m in ev.markets)
        end = max(m.close_time for m in ev.markets)
        kw: dict[str, Any] = dict(
            window=ev.window,
            std_offset_h=ev.station.std_utc_offset_hours,
            observed=observed,
            climatology=clim,
            seed_key=ev.event_ticker,
        )
        for t in decision_times(start, end):
            n_times += 1
            primary = build_composite(t=t, nbp=nbp, nbs=nbs, **kw)
            indep = build_composite(t=t, nbp=nbp, nbs=nbs, spearman=0.0, **kw)
            climo = build_composite(t=t, nbp=None, nbs=None, **kw)
            audit = _audit(primary)
            for m in ev.markets:
                if not (m.open_time <= t < m.close_time):
                    continue
                rows.append(
                    {
                        "event_ticker": ev.event_ticker,
                        "series": ev.series,
                        "kind": ev.kind,
                        "station": sid,
                        "ticker": m.ticker,
                        "strike": m.strike,
                        "t": t,
                        "p_nbm": primary.prob_above(m.strike),
                        "p_nbm_indep": indep.prob_above(m.strike),
                        "p_climo": climo.prob_above(m.strike),
                        "determined": primary.determined_above(m.strike) is not None,
                        **audit,
                    }
                )
        log.info("%s: %d decision times", ev.event_ticker, len(decision_times(start, end)))
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=FORECAST_SCHEMA), out, compression="zstd")
    return {"events": len(events), "decision_times": n_times, "rows": len(rows)}


def leakage_violations(forecasts_path: Path) -> dict[str, int]:
    """Registered guardrail: every input used at time t was public strictly before t."""
    t = pq.read_table(forecasts_path).to_pylist()
    bad_fc = sum(
        1
        for r in t
        if r["max_forecast_available_at"] is not None and r["max_forecast_available_at"] >= r["t"]
    )
    bad_obs = sum(
        1
        for r in t
        if r["max_observed_public_at"] is not None and r["max_observed_public_at"] >= r["t"]
    )
    return {
        "rows": len(t),
        "forecast_not_yet_public": bad_fc,
        "observation_not_yet_public": bad_obs,
    }


def summarize_forecasts(path: Path) -> dict[str, Any]:
    t = pq.read_table(path)
    p = np.asarray(t["p_nbm"])
    return {
        "rows": t.num_rows,
        "events": len(set(t["event_ticker"].to_pylist())),
        "determined": int(np.sum(np.asarray(t["determined"]))),
        "p_nbm_range": (float(p.min()), float(p.max())) if p.size else None,
    }
