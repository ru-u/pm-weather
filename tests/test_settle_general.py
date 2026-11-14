"""Settlement replication beyond monthly ladders: rule-text parsing, storm windows,
older NYC markets and the ACIS fallback. Fixtures are real markets and real data."""

from datetime import date
from decimal import Decimal as D

import pytest

from pmw.settle.snow import (
    check_series,
    find_station,
    parse_expiration_value,
    parse_period,
    parse_strike,
    period_total,
    summarize,
)
from pmw.utils.config import load_stations_config
from pmw.weather.acis import parse_acis_json, parse_acis_value
from pmw.weather.iem_cli import CliDay, parse_cli_json

from .conftest import load_fixture

MARKETS = load_fixture("kalshi_settled_snow_mixed.json")["markets"]
STATIONS = load_stations_config()


class FakeIem:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, date], CliDay] = {}
        for name in ("iem_cli_mixed.json", "iem_cli_KNYC_2025-12_to_2026-03.json"):
            for d in parse_cli_json(load_fixture(name)):
                self.rows[(d.station, d.valid)] = d

    def days(self, station: str, start: date, end: date) -> dict[date, CliDay]:
        return {v: r for (s, v), r in self.rows.items() if s == station and start <= v <= end}


class FakeAcis:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.kbos = parse_acis_json(load_fixture("acis_KBOS_2026-03.json"))

    def snow(self, station: str, start: date, end: date) -> dict:
        self.calls.append(station)
        return self.kbos if station == "KBOS" else {}


def _event(ticker: str) -> list[dict]:
    return [m for m in MARKETS if m["event_ticker"] == ticker]


@pytest.mark.parametrize(
    ("text", "event", "first", "last"),
    [
        ("in Boston from Jan 24, 2026 through Jan 26, 2026 is", "KXSNOWSTORM-26JANBOS",
         date(2026, 1, 24), date(2026, 1, 26)),
        ("in New York City in Jan 30, 2026 through Feb 3rd, 2026 is", "KXSNOWSTORM-26FEBNYC",
         date(2026, 1, 30), date(2026, 2, 3)),
        ("in Washington DC in Feb 21, 2026 - Feb 24, 2026 is", "KXSNOWSTORM-26FEBDC2",
         date(2026, 2, 21), date(2026, 2, 24)),
        # The first NYC market did not cover a full month: Nov 8 to Nov 30.
        ("between November 8, 2021 and November 30, 2021 (inclusive) is", "SNOWNYM-21NOV",
         date(2021, 11, 8), date(2021, 11, 30)),
        ("between December 01, 2021 and December 31, 2021 (inclusive)", "SNOWNYM-21DEC",
         date(2021, 12, 1), date(2021, 12, 31)),
        ("total snowfall in New York City in December 2026 is strictly", "KXNYCSNOWM-26DEC",
         date(2026, 12, 1), date(2026, 12, 31)),
        ("no dates here at all", "KXNYCSNOWM-26FEB", date(2026, 2, 1), date(2026, 2, 28)),
    ],
)  # fmt: skip
def test_parse_period(text, event, first, last):
    period = parse_period(text, event)
    assert (period[0], period[-1]) == (first, last)


def test_parse_period_from_real_rule_text():
    for m in MARKETS:
        period = parse_period(m["rules_primary"], m["event_ticker"])
        assert 3 <= len(period) <= 31, m["ticker"]


def test_parse_strike_new_and_old_markets():
    new = _event("KXSNOWSTORM-26JANBOS")[0]
    assert parse_strike(new) == ("greater", D(str(new["floor_strike"])), None)
    old = _event("SNOWNYM-22JAN")[0]  # no strike_type or floor_strike fields at all
    assert old.get("strike_type") is None
    assert parse_strike(old) == ("greater", D("10"), None)
    with pytest.raises(ValueError):
        parse_strike({"ticker": "X", "rules_primary": "nothing useful"})


def test_station_comes_from_rule_text_before_series_default():
    boston_storm = _event("KXSNOWSTORM-26JANBOS")[0]
    assert find_station(boston_storm, "KXSNOWSTORM", STATIONS).cli_id == "KBOS"
    dc_storm = _event("KXSNOWSTORM-26FEBDC2")[0]
    assert find_station(dc_storm, "KXSNOWSTORM", STATIONS).cli_id == "KDCA"
    # Rules name a station we have not configured: refuse rather than guess.
    odd = {"rules_primary": "", "rules_secondary": "Data for CLIZZZ can be found ..."}
    assert find_station(odd, "KXNYCSNOWM", STATIONS) is None
    # No station in the text: the series default (Central Park for NYC).
    assert find_station({"rules_primary": "x"}, "KXSNOWNYM", STATIONS).cli_id == "KNYC"
    assert find_station({"rules_primary": "x"}, "KXUNKNOWN", STATIONS) is None


def test_parse_expiration_value():
    assert parse_expiration_value("7.1 inches").inches == D("7.1")
    assert parse_expiration_value("2").inches == D("2")
    assert parse_expiration_value("T").trace
    assert parse_expiration_value("") is None and parse_expiration_value("n/a") is None


def test_acis_values():
    assert parse_acis_value("0.0").inches == 0
    assert parse_acis_value("T").trace
    assert parse_acis_value("M").missing
    assert parse_acis_value("1.2A").missing  # flagged multi-day accumulation: not a daily value
    days = parse_acis_json(load_fixture("acis_KBOS_2026-03.json"))
    assert len(days) == 31 and not any(v.missing for v in days.values())


def test_storm_windows_and_old_markets_reproduce_without_fallback():
    iem = FakeIem()
    for series, event, total in (
        ("KXSNOWSTORM", "KXSNOWSTORM-26JANBOS", "23.2"),
        ("KXSNOWSTORM", "KXSNOWSTORM-26FEBDC2", "1.0"),
        ("KXSNOWNYM", "SNOWNYM-22JAN", "15.3"),
        ("KXSNOWNYM", "KXSNOWNYM-25JAN", "3.0"),
    ):
        checks = check_series(None, iem, series, STATIONS, None, markets=_event(event))  # type: ignore[arg-type]
        assert checks and all(c.match for c in checks), event
        assert checks[0].total.inches == D(total) and checks[0].total.n_filled == 0
    # Trace-only periods: nothing above any positive strike.
    for event in ("KXSNOWSTORM-26FEBNYC", "SNOWNYM-21NOV", "SNOWNYM-23NOV"):
        checks = check_series(None, iem, "KXSNOWNYM", STATIONS, None, markets=_event(event))  # type: ignore[arg-type]
        assert all(c.match and c.computed is False for c in checks), event
        assert checks[0].total.shown == "T"


def test_fallback_fills_missing_cli_days_and_is_only_used_when_needed():
    iem, acis = FakeIem(), FakeAcis()
    boston_march = _event("KXBOSSNOWM-26MAR")
    without = check_series(None, iem, "KXBOSSNOWM", STATIONS, None, markets=boston_march)  # type: ignore[arg-type]
    assert summarize(without)["undecided"] == 5  # CLI alone cannot prove the Nos
    filled = check_series(None, iem, "KXBOSSNOWM", STATIONS, acis, markets=boston_march)  # type: ignore[arg-type]
    s = summarize(filled)
    assert s["reproduced"] == s["markets"] == 6 and s["undecided"] == 0
    assert filled[0].total.n_filled == 5 and filled[0].total.inches == D("1.9")
    assert filled[0].value_agrees is True  # Kalshi recorded 1.9 as well
    # A period with complete CLI data never touches the fallback source.
    acis.calls.clear()
    check_series(None, iem, "KXSNOWSTORM", STATIONS, acis, markets=_event("KXSNOWSTORM-26JANBOS"))  # type: ignore[arg-type]
    assert acis.calls == []


def test_fallback_value_never_overrides_a_cli_value():
    iem = FakeIem()
    period = [date(2026, 1, 24), date(2026, 1, 25), date(2026, 1, 26)]
    cli = iem.days("KBOS", period[0], period[-1])
    bogus = {d: parse_acis_value("99.0") for d in period}
    assert period_total(cli, period, bogus).inches == D("23.2")
