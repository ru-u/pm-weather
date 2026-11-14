from datetime import UTC, date, datetime

import pytest

from pmw.utils.timeutil import (
    climate_date_for,
    climate_day_bounds_utc,
    date_range,
    month_days,
    parse_ts,
)

EASTERN_STD = -5
CENTRAL_STD = -6


def test_climate_day_is_standard_time_in_winter():
    start, end = climate_day_bounds_utc(date(2026, 1, 15), EASTERN_STD)
    assert start == datetime(2026, 1, 15, 5, 0, tzinfo=UTC)
    assert end == datetime(2026, 1, 16, 5, 0, tzinfo=UTC)


def test_climate_day_does_not_shift_with_daylight_time():
    # July 4 in New York is on EDT (UTC-4), but the climate day still starts at
    # 05:00Z, which is 1:00 AM on the wall clock, and ends at 12:59 AM EDT.
    start, end = climate_day_bounds_utc(date(2026, 7, 4), EASTERN_STD)
    assert start == datetime(2026, 7, 4, 5, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 5, 5, 0, tzinfo=UTC)


def test_instant_just_after_local_midnight_in_summer_belongs_to_previous_climate_day():
    # 12:30 AM EDT on July 5 = 04:30Z = 11:30 PM EST on July 4.
    ts = datetime(2026, 7, 5, 4, 30, tzinfo=UTC)
    assert climate_date_for(ts, EASTERN_STD) == date(2026, 7, 4)
    assert climate_date_for(datetime(2026, 7, 5, 5, 0, tzinfo=UTC), EASTERN_STD) == date(2026, 7, 5)


def test_dst_transition_days_are_still_24_hours():
    for d in (date(2026, 3, 8), date(2026, 11, 1)):  # US spring-forward, fall-back
        start, end = climate_day_bounds_utc(d, CENTRAL_STD)
        assert (end - start).total_seconds() == 86400


def test_climate_date_requires_aware_timestamp():
    with pytest.raises(ValueError):
        climate_date_for(datetime(2026, 1, 1, 0, 0), EASTERN_STD)  # noqa: DTZ001


def test_parse_ts_handles_kalshi_formats():
    assert parse_ts("2027-01-01T04:59:00Z") == datetime(2027, 1, 1, 4, 59, tzinfo=UTC)
    assert parse_ts("2026-10-02T15:12:30.998305Z") == datetime(
        2026, 10, 2, 15, 12, 30, 998305, tzinfo=UTC
    )
    # 5-digit and 9-digit fractions both appear in real responses.
    assert parse_ts("2026-11-14T05:48:53.48229Z") == datetime(
        2026, 11, 14, 5, 48, 53, 482290, tzinfo=UTC
    )
    assert parse_ts("2026-11-14T05:48:53.482291234Z").microsecond == 482291
    assert parse_ts("2026-11-14T00:48:53-05:00") == datetime(2026, 11, 14, 5, 48, 53, tzinfo=UTC)
    assert parse_ts("") is None
    assert parse_ts(None) is None


def test_parse_ts_rejects_naive():
    with pytest.raises(ValueError):
        parse_ts("2026-11-14T05:48:53")


def test_month_days_and_ranges():
    assert len(month_days(2026, 2)) == 28
    assert len(month_days(2028, 2)) == 29
    assert month_days(2026, 12)[-1] == date(2026, 12, 31)
    assert date_range(date(2026, 11, 12), date(2026, 11, 15)) == [
        date(2026, 11, d) for d in (12, 13, 14, 15)
    ]
