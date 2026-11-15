"""NBM composite: known-answer checks, alignment, and the no-lookahead rules."""

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from scipy.stats import spearmanr

from pmw.models.nbm_composite import (
    OBS_LAG,
    Climatology,
    NbpForecast,
    NbpIndex,
    NbsForecast,
    NbsIndex,
    ObservedDay,
    ar1_uniforms,
    build_composite,
    quantile_values,
    season_of,
    shift_to_season,
)
from pmw.utils.timeutil import climate_day_bounds_utc, date_range

EST = -5.0
PCT = (0.0, 1.0, 2.0, 4.0, 8.0)


def _utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


def _flat_climo(value: float = 0.0, first: int = 1991, last: int = 2026) -> Climatology:
    days = date_range(date(first, 7, 1), date(last, 6, 30))
    return Climatology({d: value for d in days})


def _nbp(ends: list[datetime], pct=PCT, available_at: datetime | None = None) -> NbpIndex:
    idx = NbpIndex()
    for e in ends:
        cyc = e - timedelta(hours=35)
        idx.add(e, NbpForecast(cyc, available_at or cyc + timedelta(minutes=80), "V4.3", pct))
    return idx.freeze()


# ---------- one period ----------


def test_quantile_function_hits_the_percentiles_and_has_a_point_mass_at_zero():
    u = np.array([0.05, 0.10, 0.25, 0.50, 0.75, 0.90])
    assert np.allclose(quantile_values(u, PCT), [0.0, 0.0, 1.0, 2.0, 4.0, 8.0])
    zeros = quantile_values(np.linspace(0.001, 0.899, 50), (0, 0, 0, 0, 0))
    assert np.all(zeros == 0)
    grid = np.linspace(0.001, 0.999, 999)
    assert np.all(np.diff(quantile_values(grid, PCT)) >= 0)  # monotone


def test_tail_above_the_90th_is_exponential_with_matched_scale():
    scale = (8.0 - 4.0) / np.log(2.5)
    # P(X > q90 + scale * ln 2) = 0.05 for an exponential tail holding 10% of mass.
    x = quantile_values(np.array([0.95]), PCT)[0]
    assert x == pytest.approx(8.0 + scale * np.log(2.0))
    # A "90th percentile = 0" period still has a small chance of snow.
    assert quantile_values(np.array([0.99]), (0, 0, 0, 0, 0))[0] > 0


def test_single_full_period_reproduces_its_own_percentiles():
    # Std offset -12 makes the climate day run 12Z to 12Z, exactly one NBM period.
    window = [date(2026, 1, 26)]
    t = _utc(2026, 1, 25, 6)
    comp = build_composite(
        window=window,
        std_offset_h=-12.0,
        t=t,
        observed={},
        climatology=_flat_climo(),
        nbp=_nbp([_utc(2026, 1, 27, 12)], available_at=_utc(2026, 1, 24)),
        nbs=None,
        n=200_000,
        seed_key="x",
    )
    assert [p.weight for p in comp.forecast] == [1.0] and not comp.climo
    # P(reported > K) = P(X >= K + 0.05); at K = q - 0.05 that is P(X >= q).
    assert comp.prob_above(1.95) == pytest.approx(0.50, abs=0.005)
    assert comp.prob_above(3.95) == pytest.approx(0.25, abs=0.005)
    assert comp.prob_above(7.95) == pytest.approx(0.10, abs=0.004)
    assert comp.prob_above(-0.05) == 1.0


def test_copula_has_the_requested_rank_correlation():
    rng = np.random.default_rng(1)
    u = ar1_uniforms(rng, 100_000, 3, 0.5)
    assert spearmanr(u[:, 0], u[:, 1]).statistic == pytest.approx(0.5, abs=0.01)
    assert spearmanr(u[:, 1], u[:, 2]).statistic == pytest.approx(0.5, abs=0.01)
    ind = ar1_uniforms(rng, 100_000, 2, 0.0)
    assert abs(spearmanr(ind[:, 0], ind[:, 1]).statistic) < 0.01


def test_sum_is_calibrated_when_the_world_follows_the_model():
    """Known answer: draw 'truth' from the model's own distribution many times and
    check the composite's exceedance probabilities match observed frequencies."""
    rng = np.random.default_rng(7)
    window = [date(2026, 1, d) for d in (26, 27, 28)]
    ends = [_utc(2026, 1, d, 12) for d in (27, 28, 29)]
    comp = build_composite(
        window=window,
        std_offset_h=-12.0,
        t=_utc(2026, 1, 25, 6),
        observed={},
        climatology=_flat_climo(),
        nbp=_nbp(ends, available_at=_utc(2026, 1, 24)),
        nbs=None,
        n=100_000,
        seed_key="cal",
    )
    truth = np.zeros(100_000)
    u = ar1_uniforms(rng, 100_000, 3, 0.5)
    for j in range(3):
        truth += quantile_values(u[:, j], PCT)
    for k in (1.0, 5.0, 10.0, 20.0):
        assert comp.prob_above(k) == pytest.approx(np.mean(truth >= k + 0.05), abs=0.01)


# ---------- alignment ----------


def test_eastern_window_edges_get_partial_weights_by_hours_without_nbs():
    window = date_range(date(2026, 1, 24), date(2026, 1, 26))  # 05Z Jan 24 to 05Z Jan 27
    ends = [_utc(2026, 1, d, 12) for d in (24, 25, 26, 27)]
    comp = build_composite(
        window=window,
        std_offset_h=EST,
        t=_utc(2026, 1, 22, 20),
        observed={},
        climatology=_flat_climo(),
        nbp=_nbp(ends, available_at=_utc(2026, 1, 22, 14)),
        nbs=None,
        n=1000,
    )
    got = [(p.valid_end.day, round(p.weight, 4), p.weight_source) for p in comp.forecast]
    assert got == [
        (24, 0.2917, "hours"),
        (25, 1.0, "full"),
        (26, 1.0, "full"),
        (27, 0.7083, "hours"),
    ]
    assert not comp.climo
    assert sum(p.weight for p in comp.forecast) == pytest.approx(3.0)  # 72 hours of window


def test_nbs_shares_move_snow_to_where_the_six_hour_forecast_puts_it():
    window = [date(2026, 1, 25)]  # 05Z Jan 25 to 05Z Jan 26
    ends = [_utc(2026, 1, 25, 12), _utc(2026, 1, 26, 12)]
    s06 = {
        _utc(2026, 1, 24, 18): 0.0, _utc(2026, 1, 25, 0): 0.0,
        _utc(2026, 1, 25, 6): 1.0, _utc(2026, 1, 25, 12): 3.0,  # period ending 12Z Jan 25
        _utc(2026, 1, 25, 18): 4.0, _utc(2026, 1, 26, 0): 4.0,
        _utc(2026, 1, 26, 6): 2.0, _utc(2026, 1, 26, 12): 0.0,  # period ending 12Z Jan 26
    }  # fmt: skip
    nbs = NbsIndex([NbsForecast(_utc(2026, 1, 24, 1), _utc(2026, 1, 24, 2, 30), s06)])
    comp = build_composite(
        window=window,
        std_offset_h=EST,
        t=_utc(2026, 1, 24, 14),
        observed={},
        climatology=_flat_climo(),
        nbp=_nbp(ends, available_at=_utc(2026, 1, 24, 3)),
        nbs=nbs,
        n=1000,
    )
    w = {p.valid_end.day: (round(p.weight, 4), p.weight_source) for p in comp.forecast}
    # Period ending 12Z Jan 25: 7 h inside (05Z-12Z) -> 1/6 of the 00-06Z block (1.0)
    # plus all of 06-12Z (3.0), over a 4.0 total.
    assert w[25] == (round((1.0 / 6 + 3.0) / 4.0, 4), "s06")
    # Period ending 12Z Jan 26: 17 h inside (12Z-05Z) -> 4 + 4 + 5/6 * 2 of 10.
    assert w[26] == (round((4.0 + 4.0 + 2.0 * 5 / 6) / 10.0, 4), "s06")


def test_days_beyond_the_forecast_use_climatology():
    window = date_range(date(2026, 1, 1), date(2026, 1, 31))
    ends = [_utc(2026, 1, d, 12) for d in range(1, 11)]  # forecasts only to 12Z Jan 10
    comp = build_composite(
        window=window,
        std_offset_h=EST,
        t=_utc(2025, 12, 31, 20),
        observed={},
        climatology=_flat_climo(1.0),
        nbp=_nbp(ends, pct=(0, 0, 0, 0, 0), available_at=_utc(2025, 12, 31, 14)),
        nbs=None,
        n=2000,
    )
    climo_days = [c.day for c in comp.climo]
    assert climo_days[0] == date(2026, 1, 10) and climo_days[-1] == date(2026, 1, 31)
    assert comp.climo[0].weight == pytest.approx(17 / 24)  # 12Z to 05Z of Jan 10-11
    # Every climatology day contributes 1.0 in: 21 full days + 17/24.
    assert np.median(comp.samples) == pytest.approx(21 + 17 / 24, abs=0.3)


# ---------- no lookahead ----------


def test_forecasts_not_yet_public_are_never_used():
    window = [date(2026, 1, 26)]
    end = _utc(2026, 1, 27, 12)
    idx = NbpIndex()
    idx.add(end, NbpForecast(_utc(2026, 1, 25, 1), _utc(2026, 1, 25, 2, 30), "V4.3", PCT))
    big = (10.0, 12.0, 14.0, 16.0, 20.0)
    idx.add(end, NbpForecast(_utc(2026, 1, 25, 7), _utc(2026, 1, 25, 8, 40), "V4.3", big))
    idx.freeze()
    kw = dict(
        window=window, std_offset_h=-12.0, observed={}, climatology=_flat_climo(), nbs=None, n=500
    )
    before = build_composite(t=_utc(2026, 1, 25, 8, 30), nbp=idx, **kw)  # 07Z not public yet
    after = build_composite(t=_utc(2026, 1, 25, 8, 41), nbp=idx, **kw)
    assert before.forecast[0].cycle == _utc(2026, 1, 25, 1)
    assert after.forecast[0].cycle == _utc(2026, 1, 25, 7)
    exactly = build_composite(t=_utc(2026, 1, 25, 8, 40), nbp=idx, **kw)
    assert exactly.forecast[0].cycle == _utc(2026, 1, 25, 1)  # strictly before t
    for c in (before, after, exactly):
        assert all(p.available_at < c.t for p in c.forecast)


def test_observed_days_count_only_once_their_report_is_public():
    window = date_range(date(2026, 1, 24), date(2026, 1, 26))
    observed = {d: ObservedDay(5.0) for d in window}
    end_24 = climate_day_bounds_utc(date(2026, 1, 24), EST)[1]  # 05Z Jan 25
    kw = dict(
        window=window,
        std_offset_h=EST,
        observed=observed,
        climatology=_flat_climo(),
        nbp=None,
        nbs=None,
        n=200,
    )
    early = build_composite(t=end_24 + OBS_LAG, **kw)  # not strictly after: not public
    late = build_composite(t=end_24 + OBS_LAG + timedelta(minutes=1), **kw)
    assert early.observed_days == [] and late.observed_days == [date(2026, 1, 24)]
    assert late.observed_in == 5.0
    assert all(p < late.t for p in late.observed_public_at)
    # A missing day stops the observed prefix (later days are forecast, not skipped).
    gap = {**observed, date(2026, 1, 24): ObservedDay(None)}
    g = build_composite(**{**kw, "observed": gap, "t": _utc(2026, 2, 1)})
    assert g.observed_days == [] and len(g.climo) == 3


def test_determination():
    window = [date(2026, 1, 24)]
    observed = {date(2026, 1, 24): ObservedDay(3.0)}
    done = build_composite(
        window=window, std_offset_h=EST, t=_utc(2026, 1, 26), observed=observed,
        climatology=_flat_climo(), nbp=None, nbs=None, n=100,
    )  # fmt: skip
    assert done.fully_observed
    assert done.determined_above(2.0) is True and done.determined_above(3.0) is False
    assert done.prob_above(2.0) == 1.0 and done.prob_above(3.0) == 0.0


def test_climatology_uses_only_earlier_seasons():
    daily = {}
    for d in date_range(date(1991, 7, 1), date(2026, 6, 30)):
        daily[d] = 100.0 if season_of(d) >= 2025 else 0.0  # the target season and later
    clim = Climatology(daily)
    mat = clim.matrix([date(2026, 1, 25)], before_season=2025)
    assert mat.shape == (34, 1) and np.all(mat == 0.0)
    comp = build_composite(
        window=[date(2026, 1, 25)], std_offset_h=EST, t=_utc(2026, 1, 1),
        observed={}, climatology=clim, nbp=None, nbs=None, n=500,
    )  # fmt: skip
    assert comp.samples.max() == 0.0


def test_season_helpers():
    assert season_of(date(2025, 12, 1)) == 2025 and season_of(date(2026, 3, 1)) == 2025
    assert shift_to_season(date(2026, 2, 3), 1999) == date(2000, 2, 3)
    assert shift_to_season(date(2024, 2, 29), 2000) == date(2001, 2, 28)
