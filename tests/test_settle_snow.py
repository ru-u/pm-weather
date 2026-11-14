"""Settlement replication for SNOWOVERTIME contracts, on real CLI data and real results."""

from datetime import date
from decimal import Decimal as D

import pytest

from pmw.settle.snow import (
    PeriodTotal,
    parse_monthly_event,
    period_total,
    resolve,
)
from pmw.utils.timeutil import month_days
from pmw.weather.iem_cli import parse_amount, parse_cli_json

from .conftest import load_fixture


@pytest.fixture(scope="module")
def knyc() -> dict[date, object]:
    days = parse_cli_json(load_fixture("iem_cli_KNYC_2025-12_to_2026-03.json"))
    return {d.valid: d for d in days}


def test_parse_amount():
    assert parse_amount(1.5).inches == D("1.5")
    assert parse_amount("T").trace and parse_amount("T").inches is None
    assert parse_amount("M").missing and parse_amount(None).missing
    assert parse_amount("T").measurable == 0
    assert parse_amount(0.0).inches == 0 and not parse_amount(0.0).trace


@pytest.mark.parametrize(
    ("year", "month", "expected"),
    [(2025, 12, "7.2"), (2026, 1, "13.9"), (2026, 2, "22.3"), (2026, 3, "0")],
)
def test_monthly_totals_match_the_nws_month_to_date_figure(knyc, year, month, expected):
    period = month_days(year, month)
    total = period_total(knyc, period)
    assert total.complete
    assert total.inches == D(expected)
    # Cross-check our daily sum against the month-to-date total NWS printed in
    # the last report of the month.
    printed = knyc[period[-1]].snow_month
    assert printed.measurable == total.inches
    assert printed.trace == (total.inches == 0 and total.any_trace)


def test_reproduces_every_settled_nyc_monthly_market(knyc):
    markets = load_fixture("kalshi_historical_markets_KXNYCSNOWM.json")["markets"]
    assert len(markets) == 28
    for m in markets:
        year, month = parse_monthly_event(m["event_ticker"])
        total = period_total(knyc, month_days(year, month))
        ours = resolve(total, m["strike_type"], D(str(m["floor_strike"])), None)
        assert ours is (m["result"] == "yes"), m["ticker"]


def test_trace_is_above_zero_but_not_above_a_tenth():
    trace_only = PeriodTotal(D(0), any_trace=True, n_days=31, n_missing=0)
    assert resolve(trace_only, "greater", D(0), None) is True
    assert resolve(trace_only, "greater", D("0.1"), None) is False
    no_snow = PeriodTotal(D(0), any_trace=False, n_days=31, n_missing=0)
    assert resolve(no_snow, "greater", D(0), None) is False


def test_strictly_greater_at_the_strike_is_no():
    exactly = PeriodTotal(D("1.0"), any_trace=False, n_days=4, n_missing=0)
    assert resolve(exactly, "greater", D(1), None) is False
    assert resolve(exactly, "greater_or_equal", D(1), None) is True
    assert resolve(exactly, "between", D("0.5"), D("1.0")) is True
    assert resolve(exactly, "less", None, D("1.0")) is False


def test_missing_days_leave_a_no_undecided_but_a_yes_stands():
    partial = PeriodTotal(D("6.5"), any_trace=False, n_days=31, n_missing=3)
    assert resolve(partial, "greater", D(6), None) is True  # more snow cannot undo it
    assert resolve(partial, "greater", D(10), None) is None  # could still get there
    assert resolve(partial, "less", None, D(10)) is None


def test_missing_report_counts_as_missing(knyc):
    period = month_days(2026, 1)
    gappy = {d: v for d, v in knyc.items() if d != date(2026, 1, 25)}
    total = period_total(gappy, period)
    assert total.n_missing == 1 and not total.complete


def test_parse_monthly_event():
    assert parse_monthly_event("KXNYCSNOWM-26DEC") == (2026, 12)
    assert parse_monthly_event("KXNYCSNOWM-25DEC") == (2025, 12)
    assert parse_monthly_event("KXCHISNOWM-25DECB") == (2025, 12)
    with pytest.raises(ValueError):
        parse_monthly_event("KXSNOWSTORM-26FEBDC2")
