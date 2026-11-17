"""Weather features for the ERCOT peak model (Phase 4, v2).

Sources (both public, both joined on availability, never on issue time):
  - NBM hourly text bulletins (NBH) for the stations in configs/ercot_stations.yaml.
    For a decision at time t the cycle used is the one issued at t - 2 h
    (`NBH_LEAD`). NBH files reach the archive 35 to 100 minutes after the cycle,
    so that cycle is public by t; the join also checks `available_at < t` and
    leaves the features empty when it is not. The same rule works live.
  - ASOS observations (IEM), each usable from valid time + 15 minutes.

Stations stand for ERCOT forecast zones (houston, north, south, west; south is
the mean of Austin and San Antonio). Zones are weighted by their share of
settled load over the 90 days ending two days before the operating day, so the
weights never use the day being predicted and follow the growth of West load.

Remaining hours of operating day D after decision hour h are hour-endings
h+1 .. 24 (Central). The temperature for hour-ending HE is the forecast valid at
the end of that hour. NBH reaches 25 hours past its cycle, which can stop one
hour short of HE 24 at the 00 CT decision; the late evening is never the peak
hour, so the remaining-hours statistics use the hours that exist.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

CT = ZoneInfo("America/Chicago")
DECISION_HOURS = (0, 6, 10, 14, 17)
NBH_LEAD = timedelta(hours=2)
ZONES = ("north", "south", "west", "houston")
BASE_F = 65.0  # degree-hour base, deg F
WEIGHT_WINDOW_DAYS = 90
WEIGHT_GAP_DAYS = 2  # settled load for day D-1 may not be public at D's first decision
OBS_MAX_AGE = timedelta(hours=2)


def decision_time(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=CT).astimezone(UTC)


def nbh_cycle_for(t: datetime) -> datetime:
    """The NBH cycle used at decision time t: issued 2 hours earlier, on the hour."""
    c = t.astimezone(UTC) - NBH_LEAD
    return c.replace(minute=0, second=0, microsecond=0)


def nbh_cycle_hours_for_day(day: date) -> list[int]:
    """UTC cycle hours on UTC day `day` needed by the decisions of operating day `day`.
    Every decision of an operating day (00 to 17 CT) falls on the same UTC date."""
    out = []
    for h in DECISION_HOURS:
        c = nbh_cycle_for(decision_time(day, h))
        if c.date() != day:
            raise ValueError(f"cycle {c} for {day} {h} CT is not on {day}")
        out.append(c.hour)
    return sorted(set(out))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_nbh(root: Path, days: Iterable[date], stations: list[str]) -> pd.DataFrame:
    """Long table: station, cycle, available_at, valid, tmp, tsd (deg F)."""
    from pmw.weather.nbm_text import read_day

    return nbh_frame((rec for d in days for rec in read_day(root, "nbh", d)), stations)


def load_live_nbh(root: Path, days: Iterable[date], stations: list[str]) -> pd.DataFrame:
    """The same table from `ercot-live` snapshots: <root>/YYYYMMDD/HH.jsonl.gz per cycle."""
    import gzip
    import json

    def records() -> Iterable[dict[str, object]]:
        for d in days:
            for f in sorted((root / f"{d:%Y%m%d}").glob("*.jsonl.gz")):
                with gzip.open(f, "rt", encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            yield json.loads(line)

    return nbh_frame(records(), stations)


def nbh_frame(records: Iterable[dict[str, object]], stations: list[str]) -> pd.DataFrame:
    from pmw.weather.nbm_text import available_at, parse_nbh_block

    rows = []
    want = set(stations)
    for rec in records:
        if rec["station"] not in want or not rec["text"]:
            continue
        b = parse_nbh_block(str(rec["text"]))
        lm = rec["last_modified"]
        avail = available_at(b.issue, datetime.fromisoformat(str(lm)) if lm else None)
        tsd = b.values.get("TSD", {})
        for v, tmp in b.values.get("TMP", {}).items():
            rows.append((b.station, b.issue, avail, v, tmp, tsd.get(v)))
    df = pd.DataFrame(rows, columns=["station", "cycle", "available_at", "valid", "tmp", "tsd"])
    return df.drop_duplicates(["station", "cycle", "valid"])


def load_asos(root: Path, station_map: dict[str, str]) -> pd.DataFrame:
    """station (ICAO), valid, available_at, tmpf. `station_map`: IEM id -> ICAO."""

    texts = {
        iem: [f.read_text() for f in sorted((root / iem).glob(f"{iem}_*.csv"))]
        for iem in station_map
    }
    return asos_frame(texts, station_map)


def asos_frame(texts: dict[str, list[str]], station_map: dict[str, str]) -> pd.DataFrame:
    """The `load_asos` table from raw IEM CSV texts per IEM id (archive or live fetch)."""
    from pmw.weather.asos import parse_asos_csv

    rows = []
    for iem, icao in station_map.items():
        for text in texts.get(iem, []):
            for o in parse_asos_csv(text):
                if o.station == iem:  # a multi-station fetch holds every station's rows
                    rows.append((icao, o.valid, o.available_at, o.tmpf))
    df = pd.DataFrame(rows, columns=["station", "valid", "available_at", "tmpf"])
    return df.drop_duplicates(["station", "valid"]).sort_values(["station", "valid"])


def zone_weights(hourly: pd.DataFrame, through: date | None = None) -> pd.DataFrame:
    """Per operating day: each zone's share of settled load over the 90 days ending
    two days before it. Index oper_day (date), columns ZONES.

    `through` extends the index to a day whose own load has not settled yet (a live
    run): its weights use only days up to two days before it, as always."""
    daily = hourly.groupby("oper_day")[list(ZONES)].sum().sort_index()
    daily.index = pd.to_datetime(daily.index)
    daily = daily.asfreq("D")
    if through is not None and len(daily) and pd.Timestamp(through) > daily.index[-1]:
        daily = daily.reindex(pd.date_range(daily.index[0], pd.Timestamp(through), freq="D"))
    roll = daily.rolling(WEIGHT_WINDOW_DAYS, min_periods=14).sum().shift(WEIGHT_GAP_DAYS)
    w = roll.div(roll.sum(axis=1), axis=0)
    w.index = w.index.date
    return w.dropna()


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------


def _zone_temp(by_station: dict[str, float], zone_of: dict[str, str]) -> dict[str, float]:
    acc: dict[str, list[float]] = {}
    for st, v in by_station.items():
        if v is not None and np.isfinite(v):
            acc.setdefault(zone_of[st], []).append(v)
    return {z: float(np.mean(v)) for z, v in acc.items()}


def _weighted(zt: dict[str, float], w: pd.Series) -> float:
    if any(z not in zt for z in ZONES):
        return math.nan
    return float(sum(w[z] * zt[z] for z in ZONES))


WX_FEATURES = [
    "wx_fc_max_rem", "wx_fc_mean_rem", "wx_cdh_rem", "wx_hdh_rem", "wx_fc_tsd_rem",
    "wx_obs_now", "wx_obs_minus_fc", "wx_obs_max_today", "wx_fc_max_rem_minus_obs_now",
]  # fmt: skip


def weather_features(
    rows: pd.DataFrame,
    nbh: pd.DataFrame,
    asos: pd.DataFrame,
    weights: pd.DataFrame,
    zone_of: dict[str, str],
) -> pd.DataFrame:
    """One row per input row (needs oper_day, hour, t) with WX_FEATURES.

    - wx_fc_*: from the NBH cycle at t - 2 h, over the remaining hour-endings.
      max / mean of the load-weighted temperature; cooling and heating degree
      hours above / below 65 F per remaining hour; mean forecast spread (TSD,
      empty before NBM V4).
    - wx_obs_now: load-weighted latest observation available before t (each
      station's latest within 2 h).
    - wx_obs_minus_fc: mean over the hours between the cycle and t of observed
      minus that cycle's forecast (hourly routine obs, matched to the hour).
    - wx_obs_max_today: highest load-weighted hourly observation since 00 CT.
    """
    stations = sorted(zone_of)
    fc = {(st, c): g.set_index("valid") for (st, c), g in nbh.groupby(["station", "cycle"])}
    avail = nbh.groupby(["station", "cycle"])["available_at"].first().to_dict()
    obs_by = {st: g for st, g in asos.groupby("station")}
    obs_valid = {st: g["valid"].to_numpy("datetime64[ns]") for st, g in obs_by.items()}
    obs_avail = {st: g["available_at"].to_numpy("datetime64[ns]") for st, g in obs_by.items()}
    obs_tmp = {st: g["tmpf"].to_numpy(float) for st, g in obs_by.items()}

    out = []
    for r in rows.itertuples(index=False):
        d: date = r.oper_day
        h: int = int(r.hour)
        t = pd.Timestamp(r.t).to_pydatetime().astimezone(UTC)
        res = dict.fromkeys(WX_FEATURES, math.nan)
        if d not in weights.index:
            out.append(res)
            continue
        w = weights.loc[d]
        day0 = decision_time(d, 0)
        rem_valid = [day0 + timedelta(hours=he) for he in range(h + 1, 25)]
        cycle = nbh_cycle_for(t)

        # Forecast over the remaining hours.
        temps: dict[datetime, dict[str, float]] = {}
        spreads: dict[datetime, dict[str, float]] = {}
        fc_used: dict[str, pd.DataFrame] = {}
        for st in stations:
            key = (st, pd.Timestamp(cycle))
            g = fc.get(key)
            if g is None or not (avail[key] < t):
                continue
            fc_used[st] = g
            for v in rem_valid:
                ts = pd.Timestamp(v)
                if ts in g.index:
                    temps.setdefault(v, {})[st] = g.at[ts, "tmp"]
                    spreads.setdefault(v, {})[st] = g.at[ts, "tsd"]
        wt = [_weighted(_zone_temp(temps[v], zone_of), w) for v in rem_valid if v in temps]
        wt = [x for x in wt if np.isfinite(x)]
        if wt:
            a = np.array(wt)
            res["wx_fc_max_rem"] = float(a.max())
            res["wx_fc_mean_rem"] = float(a.mean())
            res["wx_cdh_rem"] = float(np.clip(a - BASE_F, 0, None).mean())
            res["wx_hdh_rem"] = float(np.clip(BASE_F - a, 0, None).mean())
            ws = [_weighted(_zone_temp(spreads[v], zone_of), w) for v in rem_valid if v in spreads]
            ws = [x for x in ws if np.isfinite(x)]
            if ws:
                res["wx_fc_tsd_rem"] = float(np.mean(ws))

        # Observations available before t.
        t64 = np.datetime64(t.replace(tzinfo=None), "ns")
        now: dict[str, float] = {}
        hourly_obs: dict[str, dict[datetime, float]] = {}
        for st in stations:
            if st not in obs_valid:
                continue
            n = int(np.searchsorted(obs_avail[st], t64, side="left"))  # avail < t
            if n == 0:
                continue
            last_valid = obs_valid[st][n - 1]
            if t64 - last_valid <= np.timedelta64(OBS_MAX_AGE):
                now[st] = float(obs_tmp[st][n - 1])
            # Hourly routine obs since 00 CT, matched to the top of the next hour.
            lo = int(
                np.searchsorted(
                    obs_valid[st],
                    np.datetime64((day0 - timedelta(hours=1)).replace(tzinfo=None), "ns"),
                )
            )
            hv: dict[datetime, float] = {}
            for k in range(lo, n):
                vt = pd.Timestamp(obs_valid[st][k]).to_pydatetime().replace(tzinfo=UTC)
                if vt.minute >= 45:  # routine reports at :51 to :56
                    hv[(vt + timedelta(hours=1)).replace(minute=0)] = float(obs_tmp[st][k])
            hourly_obs[st] = hv
        res["wx_obs_now"] = _weighted(_zone_temp(now, zone_of), w)

        # Observed so far today (hour-endings 1 .. h), load-weighted.
        if h > 0:
            done = [day0 + timedelta(hours=he) for he in range(1, h + 1)]
            vals = []
            for v in done:
                by = {st: hourly_obs.get(st, {}).get(v, math.nan) for st in stations}
                vals.append(_weighted(_zone_temp(by, zone_of), w))
            vals = [x for x in vals if np.isfinite(x)]
            if vals:
                res["wx_obs_max_today"] = float(max(vals))

        # Observed minus forecast over the hours between the cycle and t.
        diffs = []
        v = cycle + timedelta(hours=1)
        while v <= t:
            o = {st: hourly_obs.get(st, {}).get(v, math.nan) for st in stations}
            f = {
                st: (fc_used[st].at[pd.Timestamp(v), "tmp"]
                     if st in fc_used and pd.Timestamp(v) in fc_used[st].index else math.nan)
                for st in stations
            }  # fmt: skip
            ow = _weighted(_zone_temp(o, zone_of), w)
            fw = _weighted(_zone_temp(f, zone_of), w)
            if np.isfinite(ow) and np.isfinite(fw):
                diffs.append(ow - fw)
            v += timedelta(hours=1)
        if diffs:
            res["wx_obs_minus_fc"] = float(np.mean(diffs))
        if np.isfinite(res["wx_fc_max_rem"]) and np.isfinite(res["wx_obs_now"]):
            res["wx_fc_max_rem_minus_obs_now"] = res["wx_fc_max_rem"] - res["wx_obs_now"]
        out.append(res)
    return pd.DataFrame(out, index=rows.index)
