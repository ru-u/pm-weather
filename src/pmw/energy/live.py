"""Live ERCOT inputs: one snapshot per cron run on the `ercot-live` service (D-046).

Each run:
  1. ERCOT's public dashboard (supply-demand JSON, no key): saved verbatim, gzipped.
     `data` holds today's 5-minute system demand; points with `forecast == 1` are
     ERCOT's projection for the rest of the day and are never treated as observed.
  2. NBM hourly bulletins (NBH) for the stations in configs/ercot_stations.yaml:
     the cycles issued 1 and 2 hours before the run, if not stored yet. Block
     offsets persist in the bucket so a run does not re-download a 28 MB file.
  3. ERCOT archives through `jobs.backfill` (ERCOT credentials, Railway US only):
     every NP3-565-CD forecast posting of today and yesterday, plus NP3-910-ER
     real-time load and NP6-346-CD actuals of the last three days. Same paths as
     the history backfill, so `make pull-data PREFIX=raw/ercot` picks them up.
  4. Kalshi incentive programs (public), active and upcoming, once per UTC day.

Only step 3 needs credentials; the run skips it when none are configured.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any

import httpx

from pmw.utils.storage import Storage

log = logging.getLogger(__name__)

DASHBOARD_URL = "https://www.ercot.com/api/1/services/read/dashboards/supply-demand.json"
INCENTIVES_URL = "https://external-api.kalshi.com/trade-api/v2/incentive_programs"
LIVE_ROOT = "live/ercot"
NBH_ROOT = "live/nbm_ercot/nbh"
NBH_OFFSETS = "live/nbm_ercot/nbh_offsets.json"
NBH_LEADS_H = (1, 2)
USER_AGENT = "pmw-research/0.1"


# ---------- dashboard ----------


@dataclass(frozen=True)
class DashPoint:
    ts: datetime  # interval start, UTC
    demand_mw: float
    projected: bool  # True for ERCOT's projection of the rest of the day


def parse_dashboard(payload: dict[str, Any]) -> list[DashPoint]:
    out = []
    for p in payload.get("data", []):
        if p.get("demand") is None or p.get("timestamp") is None:
            continue
        ts = datetime.strptime(p["timestamp"], "%Y-%m-%d %H:%M:%S%z").astimezone(UTC)
        out.append(DashPoint(ts, float(p["demand"]), bool(p.get("forecast"))))
    return out


def dashboard_last_updated(payload: dict[str, Any]) -> datetime:
    return datetime.strptime(payload["lastUpdated"], "%Y-%m-%d %H:%M:%S%z").astimezone(UTC)


def dashboard_points(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Observed 5-minute points from saved snapshots, each with when we first saw it.

    `snapshots` are the saved wrappers ({"fetched_utc", "payload"}). Projected
    points are dropped. A point seen in several snapshots keeps the value and the
    fetch time of the earliest one, so a consumer can ask what was visible at t
    (`first_seen_utc < t`). Operating day and hour ending follow NP3-910-ER: local
    Central date, local hour + 1, and the second pass through the fall-back hour
    is `repeated`.
    """
    from zoneinfo import ZoneInfo

    central = ZoneInfo("America/Chicago")
    seen: dict[datetime, dict[str, Any]] = {}
    for snap in sorted(snapshots, key=lambda s: s["fetched_utc"]):
        fetched = datetime.fromisoformat(snap["fetched_utc"]).astimezone(UTC)
        for p in parse_dashboard(snap["payload"]):
            if p.projected or p.ts in seen:
                continue
            local = p.ts.astimezone(central)
            seen[p.ts] = {
                "ts_utc": p.ts, "oper_day": local.date(), "hour_ending": local.hour + 1,
                "repeated": bool(local.fold), "system": p.demand_mw, "first_seen_utc": fetched,
            }  # fmt: skip
    return [seen[k] for k in sorted(seen)]


def load_dashboard_snapshots(paths: list[Any]) -> list[dict[str, Any]]:
    out = []
    for path in paths:
        with gzip.open(path, "rt") as f:
            out.append(json.load(f))
    return out


def dashboard_path(fetched: datetime) -> str:
    return f"{LIVE_ROOT}/dashboard/{fetched:%Y/%m/%d}/{fetched:%H%M%S}Z.json.gz"


def snapshot_dashboard(http: httpx.Client, storage: Storage, now: datetime) -> dict[str, Any]:
    resp = http.get(DASHBOARD_URL)
    resp.raise_for_status()
    payload = resp.json()
    points = parse_dashboard(payload)  # refuse to save something that does not parse
    observed = [p for p in points if not p.projected]
    record = {"fetched_utc": now.isoformat(), "payload": payload}
    path = dashboard_path(now)
    storage.write_bytes(path, gzip.compress(json.dumps(record).encode()))
    return {
        "path": path,
        "last_updated": dashboard_last_updated(payload).isoformat(),
        "observed_points": len(observed),
        "last_observed_mw": observed[-1].demand_mw if observed else None,
    }


# ---------- NBM hourly bulletins ----------


def nbh_cycles(now: datetime) -> list[datetime]:
    top = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return [top - timedelta(hours=h) for h in NBH_LEADS_H]


def nbh_path(cycle: datetime) -> str:
    return f"{NBH_ROOT}/{cycle:%Y%m%d}/{cycle:%H}.jsonl.gz"


def _load_offsets(storage: Storage) -> dict[tuple[int, str], tuple[int, int]]:
    if not storage.exists(NBH_OFFSETS):
        return {}
    raw = json.loads(storage.read_bytes(NBH_OFFSETS))
    return {(int(h), s): (int(o), int(n)) for h, s, o, n in raw}


def _save_offsets(storage: Storage, offsets: dict[tuple[int, str], tuple[int, int]]) -> None:
    rows = [[h, s, o, n] for (h, s), (o, n) in sorted(offsets.items())]
    storage.write_bytes(NBH_OFFSETS, json.dumps(rows).encode())


def snapshot_nbh(
    storage: Storage,
    now: datetime,
    stations: list[str],
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    from pmw.weather.nbm_text import NbmTextArchive

    arc = NbmTextArchive("nbh", workers=len(stations), transport=transport)
    arc.offsets = _load_offsets(storage)
    saved, skipped, missing = [], 0, 0
    try:
        for cycle in nbh_cycles(now):
            path = nbh_path(cycle)
            if storage.exists(path):
                skipped += 1
                continue
            blocks = arc.fetch_cycle(cycle, stations)
            if all(b.text is None for b in blocks):
                missing += 1  # not posted yet; a later run picks it up
                continue
            lines = [
                json.dumps(
                    {
                        "product": "nbh",
                        "station": b.station,
                        "cycle": b.cycle.isoformat(),
                        "last_modified": b.last_modified.isoformat() if b.last_modified else None,
                        "how": b.how,
                        "text": b.text,
                        "fetched_utc": now.isoformat(),
                    }
                )
                for b in blocks
            ]
            storage.write_bytes(path, gzip.compress(("\n".join(lines) + "\n").encode()))
            saved.append(path)
        _save_offsets(storage, arc.offsets)
    finally:
        arc.close()
    return {
        "saved": saved,
        "already_had": skipped,
        "not_posted": missing,
        "full_downloads": arc.full_downloads,
        "range_requests": arc.range_requests,
    }


# ---------- ERCOT archives (credentials) ----------


LIVE_ARCHIVES = (("np3-565-cd", 1), ("np3-910-er", 3), ("np6-346-cd", 3))  # (EMIL, days back)


def refresh_archives(client: Any, storage: Storage, today_ct: date) -> dict[str, Any]:
    from pmw.energy.jobs import backfill

    out = {}
    for emil, back in LIVE_ARCHIVES:
        out[emil] = backfill(client, storage, emil, today_ct - timedelta(days=back), today_ct)
    return out


# ---------- Kalshi incentive programs (public) ----------


def incentives_path(day: date) -> str:
    return f"live/kalshi/incentives/{day:%Y/%m}/{day:%Y%m%d}.json.gz"


def snapshot_incentives(http: httpx.Client, storage: Storage, now: datetime) -> dict[str, Any]:
    path = incentives_path(now.date())
    if storage.exists(path):
        return {"path": path, "already_had": True}
    programs: list[dict[str, Any]] = []
    for status in ("active", "upcoming"):
        cursor: str | None = None
        for _ in range(50):  # about 8 pages at most today; the cap stops a runaway loop
            params: dict[str, str | int] = {"limit": 1000, "status": status}
            if cursor:
                params["cursor"] = cursor
            resp = http.get(INCENTIVES_URL, params=params)
            resp.raise_for_status()
            body = resp.json()
            page = body.get("incentive_programs") or []
            for p in page:
                p["_status"] = status
            programs += page
            cursor = body.get("next_cursor")
            time.sleep(0.5)  # well under Kalshi's public rate limit
            if not cursor or not page:
                break
    record = {"fetched_utc": now.isoformat(), "programs": programs}
    storage.write_bytes(path, gzip.compress(json.dumps(record).encode()))
    ours = sorted(
        {
            p["series_ticker"]
            for p in programs
            if "ERCOT" in p["series_ticker"] or "SNOW" in p["series_ticker"]
        }
    )
    return {"path": path, "programs": len(programs), "ercot_or_snow_series": ours}


# ---------- one run ----------


def run_once(
    storage: Storage,
    now: datetime,
    stations: list[str],
    ercot_client: Any | None,
    http: httpx.Client | None = None,
    nbh_transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """One snapshot. Each part fails on its own: a failure is recorded, not raised."""
    from zoneinfo import ZoneInfo

    from pmw.energy.jobs import with_retry

    own = http is None
    http = http or httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT})
    summary: dict[str, Any] = {"run_utc": now.isoformat()}
    parts: list[tuple[str, Any]] = [
        ("dashboard", partial(snapshot_dashboard, http, storage, now)),
        ("nbh", partial(snapshot_nbh, storage, now, stations, nbh_transport)),
        ("incentives", partial(snapshot_incentives, http, storage, now)),
    ]
    if ercot_client is not None:
        today_ct = now.astimezone(ZoneInfo("America/Chicago")).date()
        parts.append(("archives", partial(refresh_archives, ercot_client, storage, today_ct)))
    else:
        summary["archives"] = {"skipped": "no ERCOT credentials on this service"}
    for name, fn in parts:
        try:
            summary[name] = fn()
        except Exception as exc:
            log.exception("live part %s failed", name)
            summary[name] = {"error": f"{type(exc).__name__}: {exc}"}
    if own:
        http.close()
    path = f"{LIVE_ROOT}/runs/{now:%Y/%m/%d}/{now:%H%M%S}Z.json"
    with_retry(partial(storage.write_bytes, path, json.dumps(summary, default=str).encode()), path)
    summary["ok"] = not any(isinstance(v, dict) and "error" in v for v in summary.values())
    return summary
