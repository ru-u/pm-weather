"""NBM composite forecast for a snowfall-total contract (Phase 2, step 4).

As registered in docs/phase2_plan.md and docs/preregistration.md, at decision
time t the contract's total snowfall over its window is

    Total = Observed   (climate days whose report was public before t)
          + Forecast   (NBM 24-hour snowfall percentiles, latest cycle public before t)
          + Climate    (resampled station history, for days NBM does not reach)

and P(Total > strike) comes from Monte Carlo over that sum.

Choices fixed here, before any price or outcome was scored (also listed in
docs/preregistration.md, "Implementation clarifications"):

* Per-period distribution: piecewise-linear quantile function through
  (0, 0) and the 10/25/50/75/90th percentiles, so zero percentiles give a point
  mass at zero; above the 90th an exponential tail whose scale matches the
  75th-to-90th spacing, floored at TAIL_SCALE_MIN (the bulletin's 0.1 in
  resolution) so "90th percentile = 0" still leaves a small chance of snow.
* Dependence between consecutive forecast periods: Gaussian copula with
  Spearman rank correlation 0.5 (primary) or 0 (registered sensitivity).
* Period alignment: an NBP period (12Z to 12Z) that only partly overlaps the
  part of the window still to forecast contributes the share of its snow that
  the NBS 6-hour forecasts put inside the overlap; without a usable NBS
  forecast, the share of hours.
* Climatology: whole-season resampling (one historical season per draw, the
  same calendar days), from seasons strictly before the contract's season, so
  multi-day storms keep their structure and no later data is used. Missing
  station-days are filled from the same calendar day in another season.
* Observed snowfall for a climate day counts as public OBS_LAG after the day
  ends. Only a prefix of the window is treated as observed.
* "Strictly greater than K" on values reported to 0.1 in means a reported total
  of at least K + 0.1, i.e. a continuous total of at least K + 0.05.
"""

from __future__ import annotations

import bisect
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
from scipy.special import ndtr

from pmw.utils.timeutil import climate_day_bounds_utc

OBS_LAG = timedelta(hours=8)
TAIL_SCALE_MIN = 0.1
SPEARMAN_PRIMARY = 0.5
N_SAMPLES = 4000
ROUNDING_HALF_STEP = 0.05
_KNOT_P = np.array([0.0, 0.10, 0.25, 0.50, 0.75, 0.90])
_PERIOD = timedelta(hours=24)
_SIX = timedelta(hours=6)


# ---------- one period's distribution ----------


def quantile_values(u: np.ndarray, pct: Sequence[float]) -> np.ndarray:
    """Map uniforms u to snowfall (inches) through the period's quantile function."""
    q = np.asarray(pct, dtype=float)
    x = np.interp(np.minimum(u, 0.90), _KNOT_P, np.concatenate([[0.0], q]))
    scale = max((q[4] - q[3]) / np.log(2.5), TAIL_SCALE_MIN)
    tail = q[4] + scale * np.log(0.10 / np.maximum(1.0 - u, 1e-12))
    return np.where(u > 0.90, tail, x)


def gaussian_r(spearman: float) -> float:
    """Pearson correlation of a Gaussian copula with the given Spearman correlation."""
    return float(2.0 * np.sin(np.pi * spearman / 6.0))


def ar1_uniforms(rng: np.random.Generator, n: int, k: int, spearman: float) -> np.ndarray:
    """n x k uniforms, consecutive columns linked by a Gaussian copula (AR(1))."""
    r = gaussian_r(spearman)
    z = rng.standard_normal((n, k))
    for j in range(1, k):
        z[:, j] = r * z[:, j - 1] + np.sqrt(1.0 - r * r) * z[:, j]
    return np.asarray(ndtr(z))


# ---------- inputs ----------


@dataclass(frozen=True)
class NbpForecast:
    cycle: datetime
    available_at: datetime
    version: str
    pct: tuple[float, float, float, float, float]  # 10/25/50/75/90th percentile, inches


class NbpIndex:
    """Forecasts for one station, by 24-hour period end, sorted by availability."""

    def __init__(self) -> None:
        self._by_end: dict[datetime, list[NbpForecast]] = {}
        self._keys: dict[datetime, list[datetime]] = {}

    def add(self, valid_end: datetime, fc: NbpForecast) -> None:
        self._by_end.setdefault(valid_end, []).append(fc)

    def freeze(self) -> NbpIndex:
        for end, lst in self._by_end.items():
            lst.sort(key=lambda f: f.available_at)
            self._keys[end] = [f.available_at for f in lst]
        return self

    def latest(self, valid_end: datetime, t: datetime) -> NbpForecast | None:
        """The most recent forecast for this period that was public strictly before t."""
        keys = self._keys.get(valid_end)
        if not keys:
            return None
        i = bisect.bisect_left(keys, t)
        return self._by_end[valid_end][i - 1] if i > 0 else None


@dataclass(frozen=True)
class NbsForecast:
    cycle: datetime
    available_at: datetime
    s06_in: dict[datetime, float | None]  # 6-hour period end -> inches


class NbsIndex:
    def __init__(self, items: list[NbsForecast]) -> None:
        self._items = sorted(items, key=lambda f: f.available_at)
        self._keys = [f.available_at for f in self._items]

    def latest(self, t: datetime) -> NbsForecast | None:
        i = bisect.bisect_left(self._keys, t)
        return self._items[i - 1] if i > 0 else None


@dataclass(frozen=True)
class ObservedDay:
    snow_in: float | None  # None = missing in every source
    trace: bool = False


class Climatology:
    """Daily snowfall history for one station: season x (month, day) matrix."""

    def __init__(self, daily: dict[date, float | None]) -> None:
        self.daily = daily
        seasons = sorted({season_of(d) for d in daily})
        self.seasons = seasons

    def matrix(self, days: list[date], before_season: int) -> np.ndarray:
        """Rows = seasons strictly before `before_season`, columns = `days` mapped into
        each season. NaN where the station has no value."""
        rows = []
        for s in self.seasons:
            if s >= before_season:
                continue
            row = []
            for d in days:
                shifted = shift_to_season(d, s)
                v = self.daily.get(shifted)
                row.append(np.nan if v is None else v)
            rows.append(row)
        return np.array(rows, dtype=float).reshape(len(rows), len(days))


def season_of(d: date) -> int:
    """Snow season label: July through June, named by the year it starts in."""
    return d.year if d.month >= 7 else d.year - 1


def shift_to_season(d: date, season: int) -> date:
    year = season + (0 if d.month >= 7 else 1)
    try:
        return d.replace(year=year)
    except ValueError:  # Feb 29 into a non-leap year
        return d.replace(year=year, day=28)


# ---------- the composite ----------


@dataclass(frozen=True)
class ForecastPiece:
    valid_end: datetime
    cycle: datetime
    available_at: datetime
    version: str
    weight: float
    weight_source: str  # "full" | "s06" | "hours"
    pct: tuple[float, float, float, float, float]


@dataclass(frozen=True)
class ClimoPiece:
    day: date
    weight: float


@dataclass
class Composite:
    t: datetime
    observed_in: float
    observed_trace: bool
    observed_days: list[date]
    observed_public_at: list[datetime]
    forecast: list[ForecastPiece]
    climo: list[ClimoPiece]
    samples: np.ndarray = field(repr=False)

    @property
    def fully_observed(self) -> bool:
        return not self.forecast and not self.climo

    def prob_above(self, strike: float) -> float:
        """P(reported total strictly greater than strike)."""
        return float(np.mean(self.samples >= strike + ROUNDING_HALF_STEP))

    def determined_above(self, strike: float) -> bool | None:
        """True if observed snow alone already exceeds the strike; False if the
        whole window is observed and it does not; None while still open."""
        if self.observed_in >= strike + ROUNDING_HALF_STEP:
            return True
        return False if self.fully_observed else None


def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    """Hours of overlap of [a0, a1) and [b0, b1)."""
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds() / 3600.0)


def _period_share(
    end: datetime, r0: datetime, r1: datetime, nbs: NbsForecast | None
) -> tuple[float, str]:
    """Share of the period (end-24h, end] that falls inside [r0, r1)."""
    start = end - _PERIOD
    hours = _overlap(start, end, r0, r1)
    if hours >= 24.0 - 1e-9:
        return 1.0, "full"
    if nbs is not None:
        ends = [start + _SIX * (i + 1) for i in range(4)]
        vals = [nbs.s06_in.get(e) for e in ends]
        if all(v is not None for v in vals):
            total = float(sum(v for v in vals if v is not None))
            if total > 0:
                inside = sum(
                    (v or 0.0) * _overlap(e - _SIX, e, r0, r1) / 6.0
                    for e, v in zip(ends, vals, strict=True)
                )
                return inside / total, "s06"
    return hours / 24.0, "hours"


def build_composite(
    *,
    window: list[date],
    std_offset_h: float,
    t: datetime,
    observed: dict[date, ObservedDay],
    climatology: Climatology,
    nbp: NbpIndex | None,
    nbs: NbsIndex | None,
    spearman: float = SPEARMAN_PRIMARY,
    n: int = N_SAMPLES,
    seed_key: str = "",
) -> Composite:
    """Distribution of the window's total snowfall as seen at time t.

    Pass nbp=None for the climatology baseline (observed + climatology only).
    """
    # 1. Observed prefix of the window.
    obs_days: list[date] = []
    obs_public: list[datetime] = []
    obs_in = 0.0
    obs_trace = False
    for d in window:
        public_at = climate_day_bounds_utc(d, std_offset_h)[1] + OBS_LAG
        o = observed.get(d)
        if public_at >= t or o is None or o.snow_in is None:
            break
        obs_days.append(d)
        obs_public.append(public_at)
        obs_in += o.snow_in
        obs_trace = obs_trace or o.trace
    w0 = climate_day_bounds_utc(window[0], std_offset_h)[0]
    w1 = climate_day_bounds_utc(window[-1], std_offset_h)[1]
    r0 = climate_day_bounds_utc(obs_days[-1], std_offset_h)[1] if obs_days else w0

    # 2. Forecast periods covering the rest of the window.
    pieces: list[ForecastPiece] = []
    uncovered: list[tuple[datetime, datetime]] = []
    if r0 < w1:
        if nbp is None:
            uncovered.append((r0, w1))
        else:
            nbs_fc = nbs.latest(t) if nbs is not None else None
            end = r0.replace(hour=12, minute=0, second=0, microsecond=0)
            if end <= r0:
                end += _PERIOD
            while end - _PERIOD < w1:
                seg0, seg1 = max(end - _PERIOD, r0), min(end, w1)
                fc = nbp.latest(end, t)
                if fc is None or any(np.isnan(v) for v in fc.pct):
                    uncovered.append((seg0, seg1))
                else:
                    share, source = _period_share(end, r0, w1, nbs_fc)
                    pieces.append(
                        ForecastPiece(
                            valid_end=end,
                            cycle=fc.cycle,
                            available_at=fc.available_at,
                            version=fc.version,
                            weight=share,
                            weight_source=source,
                            pct=fc.pct,
                        )
                    )
                end += _PERIOD

    # 3. Climatology for whatever no forecast covers, by climate day.
    climo: list[ClimoPiece] = []
    for d in window[len(obs_days) :]:
        d0, d1 = climate_day_bounds_utc(d, std_offset_h)
        hours = sum(_overlap(d0, d1, a, b) for a, b in uncovered)
        if hours > 0:
            climo.append(ClimoPiece(day=d, weight=hours / 24.0))

    # 4. Monte Carlo.
    rng = np.random.default_rng(zlib.crc32(f"{seed_key}|{t.isoformat()}".encode()))
    total = np.full(n, obs_in)
    if pieces:
        u = ar1_uniforms(rng, n, len(pieces), spearman)
        for j, p in enumerate(pieces):
            if p.weight > 0:
                total += p.weight * quantile_values(u[:, j], p.pct)
    if climo:
        days = [c.day for c in climo]
        mat = climatology.matrix(days, before_season=season_of(window[0]))
        if mat.shape[0] == 0:
            raise ValueError("no climatology seasons before this window")
        draws = mat[rng.integers(0, mat.shape[0], size=n)]
        for j in range(draws.shape[1]):
            col = mat[:, j][~np.isnan(mat[:, j])]
            holes = np.isnan(draws[:, j])
            if holes.any():
                if col.size == 0:
                    raise ValueError(f"no climatology at all for {days[j]}")
                draws[holes, j] = rng.choice(col, size=int(holes.sum()))
        weights = np.array([c.weight for c in climo])
        total += draws @ weights
    return Composite(
        t=t,
        observed_in=obs_in,
        observed_trace=obs_trace,
        observed_days=obs_days,
        observed_public_at=obs_public,
        forecast=pieces,
        climo=climo,
        samples=total,
    )
