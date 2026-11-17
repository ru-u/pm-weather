"""CLI text products from the IEM AFOS archive (every issued version)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from pmw.weather.cli_text import parse_archive, parse_product

FIX = Path(__file__).parent / "fixtures"


def _products():
    ps, bad = parse_archive((FIX / "iem_afos_CLINYC_2026-01-25.txt").read_text())
    assert bad == 0
    return ps


def test_intraday_and_final_versions_of_the_january_2026_storm():
    ps = {(p.climate_date, p.intraday): p for p in _products()}
    intraday = ps[(date(2026, 1, 25), True)]
    final = ps[(date(2026, 1, 25), False)]
    assert intraday.issue_utc == datetime(2026, 1, 25, 21, 37, tzinfo=UTC)
    assert intraday.snow.inches == Decimal("8.8")  # 4 pm: not final (D-032)
    assert final.issue_utc == datetime(2026, 1, 26, 6, 46, tzinfo=UTC)
    assert final.snow.inches == Decimal("11.4")  # "11.4 R": record flag dropped
    assert final.precip.inches == Decimal("1.80")  # "1.80R"
    assert (final.pil, final.office, final.correction) == ("CLINYC", "KOKX", False)


def test_trace_today_and_zero_yesterday():
    ps = {(p.climate_date, p.intraday): p for p in _products()}
    assert ps[(date(2026, 1, 26), True)].snow.trace is True
    assert ps[(date(2026, 1, 24), False)].snow.inches == Decimal("0.0")


def _synthetic(wmo: str, local: str, snow: str) -> str:
    return "\n".join(
        [
            "123 ", wmo, "CLIORD", "", "CLIMATE REPORT ",
            "NATIONAL WEATHER SERVICE CHICAGO IL", local, "",
            "...THE CHICAGO-OHARE CLIMATE SUMMARY FOR JANUARY 31 2026...",
            "VALID TODAY AS OF 0400 PM LOCAL TIME.", "",
            "SNOWFALL (IN)", f"  TODAY           {snow}          9.9  1990   0.3", "",
        ]
    )  # fmt: skip


def test_issue_time_crosses_a_month_boundary_in_utc():
    p = parse_product(_synthetic("CDUS43 KLOT 010030", "630 PM CST SAT JAN 31 2026", "MM"))
    assert p.issue_utc == datetime(2026, 2, 1, 0, 30, tzinfo=UTC)
    assert p.climate_date == date(2026, 1, 31) and p.intraday
    assert p.snow.missing


def test_correction_flag_and_inconsistent_times_rejected():
    p = parse_product(_synthetic("CDUS43 KLOT 312215 CCA", "415 PM CST SAT JAN 31 2026", "2.3"))
    assert p.correction and p.snow.inches == Decimal("2.3")
    with pytest.raises(ValueError):
        parse_product(_synthetic("CDUS43 KLOT 311015", "415 PM CST SAT JAN 31 2026", "2.3"))


def test_late_correction_uses_its_send_time_and_trailing_text_is_allowed():
    p = parse_product(
        _synthetic("CDUS43 KLOT 011346 CCA", "205 AM CST SAT JAN 31 2026...CORRECTION", "1.0")
    )
    assert p.correction
    assert p.issue_utc == datetime(2026, 2, 1, 13, 46, tzinfo=UTC)  # public then, not at 08Z


def test_valid_as_of_without_today_is_intraday_and_the_final_is_not():
    ps, bad = parse_archive((FIX / "iem_afos_cli_valid_as_of_variants.txt").read_text())
    assert bad == 0
    got = [(p.pil, p.climate_date, p.issue_utc.hour, p.intraday) for p in ps]
    assert got == [
        ("CLIBOS", date(2021, 11, 1), 21, True),  # "VALID AS OF 0500 PM"
        ("CLIBOS", date(2021, 11, 1), 5, False),  # the final, issued after midnight
        ("CLIAUS", date(2021, 11, 30), 13, True),  # "VALID AS OF 0740 AM"
        ("CLIORD", date(2025, 12, 1), 22, True),  # "SNOWFALL VALID AS OF 1200 PM"
    ]
