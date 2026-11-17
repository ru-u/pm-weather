"""Weather features for the ERCOT model: availability and no-lookahead checks."""

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from pmw.features.ercot_weather import (
    ZONES,
    decision_time,
    nbh_cycle_for,
    nbh_cycle_hours_for_day,
    weather_features,
    zone_weights,
)

ZONE_OF = {"KIAH": "houston", "KDFW": "north", "KAUS": "south", "KSAT": "south", "KMAF": "west"}
D = date(2024, 7, 1)  # CDT: 00 CT = 05Z


def test_cycle_is_two_hours_before_decision_across_dst():
    assert nbh_cycle_for(decision_time(D, 14)) == datetime(2024, 7, 1, 17, tzinfo=UTC)
    assert nbh_cycle_hours_for_day(date(2024, 7, 1)) == [3, 9, 13, 17, 20]
    assert nbh_cycle_hours_for_day(date(2024, 1, 2)) == [4, 10, 14, 18, 21]
    # Spring-forward Sunday: 00 CT is still CST, the rest CDT.
    assert nbh_cycle_hours_for_day(date(2024, 3, 10)) == [4, 9, 13, 17, 20]


def test_zone_weights_use_only_days_ending_two_days_before():
    days = pd.date_range("2024-01-01", periods=40, freq="D").date
    hourly = pd.DataFrame(
        {"oper_day": days, "north": 1.0, "south": 1.0, "west": 1.0, "houston": 1.0}
    )
    # A huge north load on day 30 must not move the weights of days 30 and 31.
    hourly.loc[30, "north"] = 1e6
    w = zone_weights(hourly)
    assert w.loc[days[30], "north"] == pytest.approx(0.25)
    assert w.loc[days[31], "north"] == pytest.approx(0.25)
    assert w.loc[days[32], "north"] > 0.9
    assert np.allclose(w[list(ZONES)].sum(axis=1), 1.0)


def _nbh(cycle: datetime, temp: float, avail_lag: timedelta = timedelta(minutes=50)):
    rows = []
    for st in ZONE_OF:
        for k in range(1, 26):
            v = cycle + timedelta(hours=k)
            rows.append((st, pd.Timestamp(cycle), pd.Timestamp(cycle + avail_lag), pd.Timestamp(v),
                         temp + (k == 5) * 10.0, 2.0))  # fmt: skip
    return pd.DataFrame(rows, columns=["station", "cycle", "available_at", "valid", "tmp", "tsd"])


def _asos(times_temps):
    rows = [(st, pd.Timestamp(v), pd.Timestamp(v + timedelta(minutes=15)), t)
            for st in ZONE_OF for v, t in times_temps]  # fmt: skip
    return pd.DataFrame(rows, columns=["station", "valid", "available_at", "tmpf"])


def _weights():
    return pd.DataFrame({z: [0.25] for z in ZONES}, index=[D])


def test_forecast_from_cycle_two_hours_before_and_only_remaining_hours():
    t = decision_time(D, 14)  # 19Z
    good = _nbh(datetime(2024, 7, 1, 17, tzinfo=UTC), 90.0)  # its k=5 hour (22Z = HE17) is +10
    newer = _nbh(datetime(2024, 7, 1, 18, tzinfo=UTC), 50.0)  # issued after t - 2h: never used
    rows = pd.DataFrame({"oper_day": [D], "hour": [14], "t": [pd.Timestamp(t)]})
    f = weather_features(rows, pd.concat([good, newer]), _asos([]), _weights(), ZONE_OF).iloc[0]
    assert f["wx_fc_max_rem"] == 100.0
    assert 90.0 < f["wx_fc_mean_rem"] < 100.0
    assert f["wx_cdh_rem"] > 25 and f["wx_hdh_rem"] == 0
    assert f["wx_fc_tsd_rem"] == 2.0


def test_forecast_not_yet_public_is_left_empty():
    t = decision_time(D, 14)
    late = _nbh(datetime(2024, 7, 1, 17, tzinfo=UTC), 90.0, avail_lag=timedelta(hours=2, minutes=5))
    rows = pd.DataFrame({"oper_day": [D], "hour": [14], "t": [pd.Timestamp(t)]})
    f = weather_features(rows, late, _asos([]), _weights(), ZONE_OF).iloc[0]
    assert np.isnan(f["wx_fc_max_rem"])


def test_observations_only_before_t_and_obs_minus_forecast():
    t = decision_time(D, 14)  # 19:00Z
    cyc = datetime(2024, 7, 1, 17, tzinfo=UTC)
    obs = [
        (datetime(2024, 7, 1, 5, 53, tzinfo=UTC), 80.0),  # HE1 CT
        (datetime(2024, 7, 1, 17, 53, tzinfo=UTC), 93.0),  # matched to 18Z, forecast 90
        (datetime(2024, 7, 1, 18, 53, tzinfo=UTC), 95.0),  # public 19:08Z: after t
    ]
    rows = pd.DataFrame({"oper_day": [D], "hour": [14], "t": [pd.Timestamp(t)]})
    f = weather_features(rows, _nbh(cyc, 90.0), _asos(obs), _weights(), ZONE_OF).iloc[0]
    assert f["wx_obs_now"] == 93.0
    assert f["wx_obs_max_today"] == 93.0
    assert f["wx_obs_minus_fc"] == 3.0  # only 18Z is both observed (public) and forecast
    assert f["wx_fc_max_rem_minus_obs_now"] == 100.0 - 93.0


def test_missing_zone_gives_empty_weighted_value():
    t = decision_time(D, 14)
    nbh = _nbh(datetime(2024, 7, 1, 17, tzinfo=UTC), 90.0)
    nbh = nbh[nbh["station"] != "KMAF"]  # no West station
    rows = pd.DataFrame({"oper_day": [D], "hour": [14], "t": [pd.Timestamp(t)]})
    f = weather_features(rows, nbh, _asos([]), _weights(), ZONE_OF).iloc[0]
    assert np.isnan(f["wx_fc_max_rem"])
