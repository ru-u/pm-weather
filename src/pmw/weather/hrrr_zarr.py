"""HRRR point forecasts from the public HRRR Zarr archive on AWS (bucket `hrrrzarr`).

Layout (checked 2026-11-17):
    https://hrrrzarr.s3.amazonaws.com/sfc/YYYYMMDD/YYYYMMDD_HHz_fcst.zarr/<level>/<var>/<level>/<var>/<k>.<tj>.<ti>
Each chunk holds every forecast hour of one 150 x 150 grid tile (about 450 km on
a side), blosc-compressed, so one station's forecast for one run and variable is
one HTTP GET of about 0.1 to 1 MB. The grid is HRRR's Lambert conformal 3 km grid
(1059 x 1799, row 0 in the south); projection coordinates are stored next to each
variable. The forecast-hour coordinate is per variable (`forecast_period` or
`forecast_period_2`, in hours), named in the variable's `_ARRAY_DIMENSIONS`.

Variables used: surface ASNOW and APCP accumulated since the run started (metres of
snow; kg m-2 = mm of water), and 2 m temperature (K).

Availability: a synoptic run (00/06/12/18Z, 48 h) is complete about 1 h 45 min
after its run time; `AVAILABLE_AFTER` (2 h) is used for every join.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
import numpy as np

BASE = "https://hrrrzarr.s3.amazonaws.com/sfc"
AVAILABLE_AFTER = timedelta(hours=2)
TILE = 150
DX = 3000.0
# Projection coordinates of grid point (0, 0), read from the archive's coordinate arrays.
X0, Y0 = -2697520.14252193, -1587306.15255666
NY, NX = 1059, 1799
VARIABLES = {
    "asnow_m": ("surface", "ASNOW_acc_fcst"),
    "apcp_mm": ("surface", "APCP_acc_fcst"),
    "t2m_k": ("2m_above_ground", "TMP"),
}


@dataclass(frozen=True)
class GridPoint:
    j: int  # row (south to north)
    i: int  # column (west to east)

    @property
    def tile(self) -> tuple[int, int]:
        return self.j // TILE, self.i // TILE


def grid_point(lat: float, lon: float) -> GridPoint:
    from pyproj import Proj

    p = Proj(proj="lcc", lat_0=38.5, lon_0=-97.5, lat_1=38.5, lat_2=38.5, R=6371229)
    x, y = p(lon, lat)
    j, i = round((y - Y0) / DX), round((x - X0) / DX)
    if not (0 <= j < NY and 0 <= i < NX):
        raise ValueError(f"({lat}, {lon}) is outside the HRRR grid")
    return GridPoint(j, i)


def run_url(run: datetime) -> str:
    return f"{BASE}/{run:%Y%m%d}/{run:%Y%m%d_%H}z_fcst.zarr"


def _decode(raw: bytes, dtype: str, shape: list[int]) -> np.ndarray:
    import numcodecs

    return np.frombuffer(numcodecs.Blosc().decode(raw), dtype=dtype).reshape(shape)


class HrrrZarr:
    def __init__(self, workers: int = 16, transport: httpx.BaseTransport | None = None) -> None:
        self.client = httpx.Client(
            timeout=120, headers={"User-Agent": "pmw-research/0.1"}, transport=transport,
            limits=httpx.Limits(max_connections=workers + 4),
        )  # fmt: skip
        self.workers = workers
        self.requests = 0
        self.bytes = 0

    def close(self) -> None:
        self.client.close()

    def _get(self, url: str) -> httpx.Response | None:
        for _ in range(4):
            try:
                r = self.client.get(url)
            except httpx.TransportError:
                continue
            self.requests += 1
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                self.bytes += len(r.content)
                return r
        raise RuntimeError(f"HRRR zarr failed: {url}")

    def _meta(self, base: str) -> dict[str, Any] | None:
        r = self._get(f"{base}/.zmetadata")
        return None if r is None else dict(json.loads(r.text)["metadata"])

    def point_series(
        self, run: datetime, var: str, points: dict[str, GridPoint]
    ) -> dict[str, tuple[np.ndarray, np.ndarray]] | None:
        """station -> (forecast hours, values) for one run and variable; None if absent."""
        level, name = VARIABLES[var]
        base = f"{run_url(run)}/{level}/{name}"
        meta = self._meta(base)
        if meta is None:
            return None
        arr = meta[f"{level}/{name}/.zarray"]
        dims = meta[f"{level}/{name}/.zattrs"]["_ARRAY_DIMENSIONS"]
        fp_name = "forecast_period_2" if dims[0] == "time_2" else "forecast_period"
        fp_meta = meta[f"{fp_name}/.zarray"]
        r = self._get(f"{base}/{fp_name}/0")
        if r is None:
            return None
        hours = _decode(r.content, fp_meta["dtype"], fp_meta["chunks"])[: fp_meta["shape"][0]]
        tiles = sorted({p.tile for p in points.values()})

        def fetch(tile: tuple[int, int]) -> tuple[tuple[int, int], np.ndarray | None]:
            rr = self._get(f"{base}/{level}/{name}/0.{tile[0]}.{tile[1]}")
            if rr is None:
                return tile, None
            return tile, _decode(rr.content, arr["dtype"], arr["chunks"])

        with ThreadPoolExecutor(max_workers=min(self.workers, len(tiles))) as pool:
            got = dict(pool.map(fetch, tiles))
        out = {}
        fill = arr.get("fill_value")
        for st, p in points.items():
            block = got.get(p.tile)
            if block is None:
                continue
            v = block[: len(hours), p.j % TILE, p.i % TILE].astype(float)
            if fill is not None:
                v[v == float(fill)] = np.nan
            out[st] = (hours.astype(int), v)
        return out


# ---------- pull for a set of (run, station) needs ----------


def run_for(t: datetime) -> datetime:
    """Latest synoptic (00/06/12/18Z) run complete strictly before t."""
    c = t - AVAILABLE_AFTER - timedelta(microseconds=1)
    return c.replace(hour=c.hour - c.hour % 6, minute=0, second=0, microsecond=0)


def run_path(root: Any, run: datetime) -> Any:
    return root / f"{run:%Y}" / f"{run:%m}" / f"{run:%Y%m%d%H}.json"


def pull_run(h: HrrrZarr, root: Any, run: datetime, points: dict[str, GridPoint]) -> str:
    path = run_path(root, run)
    if path.exists():
        return "had"
    rec: dict[str, Any] = {"run": run.isoformat(), "stations": {}}
    for var in VARIABLES:
        got = h.point_series(run, var, points)
        if got is None:
            continue
        for st, (hours, vals) in got.items():
            rec["stations"].setdefault(st, {})[var] = {
                "hours": [int(x) for x in hours],
                "values": [None if not np.isfinite(v) else round(float(v), 5) for v in vals],
            }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(rec))
    tmp.rename(path)
    return "saved" if rec["stations"] else "empty"
