from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from pmw.weather.asos import AVAILABILITY_LAG, parse_asos_csv, pull_year

FIX = Path(__file__).parent / "fixtures"


def test_parse_real_freeze_day_keeps_negative_temperatures_and_specials():
    obs = parse_asos_csv((FIX / "iem_asos_MAF_2021-02-15.csv").read_text())
    assert len(obs) == 30
    assert obs[0].station == "MAF" and obs[0].valid == datetime(2021, 2, 15, 0, 53, tzinfo=UTC)
    by_time = {o.valid: o.tmpf for o in obs}
    assert by_time[datetime(2021, 2, 15, 9, 53, tzinfo=UTC)] == -0.9
    assert by_time[datetime(2021, 2, 15, 1, 29, tzinfo=UTC)] == 6.0  # a special report
    assert obs[0].available_at == obs[0].valid + AVAILABILITY_LAG


def test_parse_skips_missing_and_rejects_wrong_header():
    text = "station,valid,tmpf\nIAH,2024-07-01 00:53,M\nIAH,2024-07-01 01:53,88.00\n"
    assert [o.tmpf for o in parse_asos_csv(text)] == [88.0]
    with pytest.raises(ValueError):
        parse_asos_csv("station,valid,dwpf\nIAH,2024-07-01 00:53,70\n")


def test_pull_year_skips_saved_past_years_and_refreshes_current(tmp_path):
    calls: list[httpx.QueryParams] = []
    body = (FIX / "iem_asos_MAF_2021-02-15.csv").read_text()

    def fake(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params)
        return httpx.Response(200, text=body)

    client = httpx.Client(transport=httpx.MockTransport(fake))
    today = date(2026, 11, 16)
    assert pull_year(client, tmp_path, "MAF", 2021, today=today) is not None
    assert pull_year(client, tmp_path, "MAF", 2021, today=today) is None  # saved, past year
    assert pull_year(client, tmp_path, "MAF", 2026, today=today) is not None
    assert pull_year(client, tmp_path, "MAF", 2026, today=today) is not None  # current: refetch
    assert len(calls) == 3
    assert calls[0]["year2"] == "2022" and calls[0]["day2"] == "1"  # end date is exclusive
    assert calls[1]["month2"] == "11" and calls[1]["day2"] == "17"
    assert calls[0].get_list("report_type") == ["3", "4"]
