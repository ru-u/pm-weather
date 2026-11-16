"""ERCOT client and report parsing.

Real fixtures (saved by the first `pmw ercot-probe` run on Railway, 2026-11-16):
  ercot_np6-346-cd_20261114.zip        NP6-346-CD for operating day 2026-11-14
  ercot_np3-565-cd_20261115T1930.zip   NP3-565-CD posted 2026-11-15 19:30 CT
The small synthetic CSVs below cover cases no single real file shows (DST days,
partial and revised versions); their layout is copied from the real file.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal as D

import httpx
import pytest

from pmw.energy import ercot
from pmw.energy.ercot import (
    ArchiveDoc,
    DayVersion,
    ErcotCredentials,
    ErcotError,
    ErcotHostError,
    ErcotPublic,
    daily_peak,
    expected_hours,
    first_complete,
    is_complete,
    parse_archive_page,
    parse_load_by_forecast_zone_csv,
    parse_post_datetime,
    read_doc_csv,
    require_us_railway,
    unpack_bulk_download,
)

HEADER = '"OperDay","HourEnding","NORTH","SOUTH","WEST","HOUSTON","TOTAL","DSTFlag"'


def day_csv(day: str, peak: str = "51346.64", hours: list[str] | None = None, flags=None) -> str:
    hours = hours or [f"{h:02d}:00" for h in range(1, 25)]
    flags = flags or ["N"] * len(hours)
    lines = [HEADER]
    for i, (h, f) in enumerate(zip(hours, flags, strict=True)):
        total = peak if h == "17:00" else f"{40000 + i * 10}.25"
        lines.append(f'"{day}","{h}","1.0","2.0","3.0","4.0","{total}","{f}"')
    return "\r\n".join(lines) + "\r\n"


def doc_zip(name: str, csv_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, csv_text)
    return buf.getvalue()


def bulk_zip(docs: dict[int, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for doc_id, data in docs.items():
            z.writestr(f"{doc_id}.zip", data)
    return buf.getvalue()


def doc(doc_id: int, posted: str) -> ArchiveDoc:
    return ArchiveDoc("np6-346-cd", doc_id, parse_post_datetime(posted), posted, "x")


# ---- parsing ----


def test_parse_normal_day_keeps_published_precision():
    rows = parse_load_by_forecast_zone_csv(day_csv("11/14/2026"))
    assert len(rows) == 24 and rows[0].oper_day == date(2026, 11, 14)
    assert rows[0].hour_ending == 1 and rows[-1].hour_ending == 24
    assert daily_peak(rows, date(2026, 11, 14)) == D("51346.64")  # no rounding
    assert is_complete(rows, date(2026, 11, 14))


def test_unexpected_header_fails_loudly():
    with pytest.raises(ValueError, match="unexpected NP6-346-CD header"):
        parse_load_by_forecast_zone_csv(
            '"OperDay","HourEnding","TOTAL"\r\n"11/14/2026","01:00","1"'
        )


def test_operating_day_lengths_follow_central_dst():
    assert expected_hours(date(2026, 11, 14)) == 24
    assert expected_hours(date(2026, 3, 8)) == 23  # spring forward
    assert expected_hours(date(2026, 11, 1)) == 25  # fall back


def test_dst_days_are_complete_only_with_the_right_hours():
    spring = [f"{h:02d}:00" for h in range(1, 25) if h != 3]
    rows = parse_load_by_forecast_zone_csv(day_csv("03/08/2026", hours=spring))
    assert is_complete(rows, date(2026, 3, 8))
    fall = [f"{h:02d}:00" for h in range(1, 25)]
    fall.insert(2, "02:00")
    flags = ["N"] * 25
    flags[2] = "Y"  # the repeated hour
    rows = parse_load_by_forecast_zone_csv(day_csv("11/01/2026", hours=fall, flags=flags))
    assert is_complete(rows, date(2026, 11, 1))
    # Same 25 rows without the flag: a duplicated hour, not a complete day.
    rows = parse_load_by_forecast_zone_csv(day_csv("11/01/2026", hours=fall))
    assert not is_complete(rows, date(2026, 11, 1))


def test_first_complete_version_wins_over_later_revisions():
    day = date(2026, 11, 14)
    partial = parse_load_by_forecast_zone_csv(
        day_csv("11/14/2026", hours=[f"{h:02d}:00" for h in range(1, 24)])
    )
    first = parse_load_by_forecast_zone_csv(day_csv("11/14/2026", peak="51346.64"))
    revised = parse_load_by_forecast_zone_csv(day_csv("11/14/2026", peak="51399.10"))
    versions = [
        DayVersion(doc(3, "2026-11-16T10:00:00"), revised),
        DayVersion(doc(1, "2026-11-15T05:30:00"), partial),
        DayVersion(doc(2, "2026-11-15T06:15:00"), first),
    ]
    chosen = first_complete(versions, day)
    assert chosen is not None and chosen.doc.doc_id == 2
    assert daily_peak(chosen.rows, day) == D("51346.64")
    assert first_complete(versions[1:2], day) is None


def test_blank_total_makes_a_day_incomplete():
    text = day_csv("11/14/2026").replace('"40000.25"', '""')
    rows = parse_load_by_forecast_zone_csv(text)
    assert rows[0].total is None and not is_complete(rows, date(2026, 11, 14))


def test_post_datetime_is_central_prevailing_time():
    assert parse_post_datetime("2026-11-15T05:31:07") == datetime(
        2026, 11, 15, 11, 31, 7, tzinfo=UTC
    )
    assert parse_post_datetime("2026-07-15T05:31:07.123") == datetime(
        2026, 7, 15, 10, 31, 7, tzinfo=UTC
    )


def test_archive_page_and_bulk_download_round_trip():
    payload = {
        "_meta": {"totalRecords": 2, "pageSize": 1000, "totalPages": 1, "currentPage": 1},
        "archives": [
            {"docId": 1189001, "friendlyName": "ActualLoadofForecastZones_20261114",
             "postDatetime": "2026-11-15T05:30:12",
             "_links": {"endpoint": {"href": f"{ercot.BASE_URL}/archive/np6-346-cd?download=1189001"}}},
            {"friendlyName": "ActualLoadofForecastZones_20261113",
             "postDatetime": "2026-11-14T05:30:09",
             "_links": {"endpoint": {"href": f"{ercot.BASE_URL}/archive/np6-346-cd?download=1188555"}}},
        ],
    }  # fmt: skip
    docs, pages = parse_archive_page("np6-346-cd", payload)
    assert pages == 1 and [d.doc_id for d in docs] == [1189001, 1188555]
    inner = doc_zip(
        "cdr.00013102.0000000000000000.20261115.053012.ACTUALLOADOFFORECASTZONES.csv",
        day_csv("11/14/2026"),
    )
    got = unpack_bulk_download(bulk_zip({1189001: inner}))
    name, text = read_doc_csv(got[1189001])
    assert name.endswith(".csv") and text.startswith('"OperDay"')


# ---- safety ----


def test_credentials_never_appear_in_repr_or_errors():
    creds = ErcotCredentials("user@example.com", "hunter2-secret", "subkey-secret")
    assert "secret" not in repr(creds) and "user@example.com" not in repr(creds)
    with pytest.raises(ErcotHostError) as exc:
        ErcotCredentials.from_env({"ERCOT_API_USERNAME": "u", "ERCOT_API_PASSWORD": "p-secret"})
    assert "ERCOT_PUBLIC_API_SUBSCRIPTION_KEY" in str(exc.value) and "secret" not in str(exc.value)


def test_refuses_to_run_off_railway_us():
    with pytest.raises(ErcotHostError):
        require_us_railway({})
    with pytest.raises(ErcotHostError):
        require_us_railway({"RAILWAY_REPLICA_REGION": "europe-west4-drams3a"})
    assert require_us_railway({"RAILWAY_REPLICA_REGION": "us-east4-eqdc4a"}).startswith("us-")
    with pytest.raises(ErcotHostError):
        ErcotPublic(ErcotCredentials("u", "p", "k"))  # no transport, not on Railway


# ---- client, on a mock transport ----


class FakeErcot:
    def __init__(self, fail_first: dict[str, int] | None = None) -> None:
        self.calls: list[httpx.Request] = []
        self.tokens = 0
        self.fail_first = dict(fail_first or {})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        if url.startswith(ercot.TOKEN_URL):
            self.tokens += 1
            return httpx.Response(
                200, json={"id_token": f"tok{self.tokens}", "token_type": "Bearer"}
            )
        for key, status in list(self.fail_first.items()):
            if key in url:
                del self.fail_first[key]
                return httpx.Response(status, text="slow down")
        assert request.headers["Ocp-Apim-Subscription-Key"] == "subkey"
        assert request.headers["Authorization"].startswith("Bearer tok")
        if request.method == "GET" and "/archive/np6-346-cd" in url:
            page = int(request.url.params["page"])
            payload = {
                "_meta": {"totalPages": 2, "currentPage": page},
                "archives": [{"docId": 100 + page, "friendlyName": "f",
                              "postDatetime": f"2026-11-1{page}T05:30:00"}],
            }  # fmt: skip
            return httpx.Response(200, json=payload)
        if request.method == "POST" and url.endswith("/archive/np6-346-cd/download"):
            ids = json.loads(request.content)["docIds"]
            return httpx.Response(
                200, content=bulk_zip({i: doc_zip(f"{i}.csv", day_csv("11/14/2026")) for i in ids})
            )
        return httpx.Response(404, text="nope")


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(ercot.time, "sleep", lambda s: None)


def make_client(fake: FakeErcot) -> ErcotPublic:
    return ErcotPublic(
        ErcotCredentials("user", "pw-secret", "subkey"),
        min_request_interval_s=0,
        transport=httpx.MockTransport(fake),
    )


def test_client_logs_in_once_paginates_and_downloads(no_sleep):
    fake = FakeErcot()
    c = make_client(fake)
    docs = c.archive(
        "np6-346-cd", datetime(2026, 11, 10, tzinfo=UTC), datetime(2026, 11, 13, tzinfo=UTC)
    )
    assert [d.doc_id for d in docs] == [101, 102]
    files = c.download("np6-346-cd", [101, 102])
    assert set(files) == {101, 102}
    assert fake.tokens == 1
    token_req = fake.calls[0]
    body = token_req.content.decode()
    assert "grant_type=password" in body and "response_type=id_token" in body


def test_client_retries_429_and_refreshes_on_401(no_sleep):
    fake = FakeErcot(fail_first={"page=1": 429})
    c = make_client(fake)
    assert (
        len(
            c.archive(
                "np6-346-cd", datetime(2026, 11, 10, tzinfo=UTC), datetime(2026, 11, 13, tzinfo=UTC)
            )
        )
        == 2
    )
    fake = FakeErcot(fail_first={"page=1": 401})
    c = make_client(fake)
    c.archive("np6-346-cd", datetime(2026, 11, 10, tzinfo=UTC), datetime(2026, 11, 13, tzinfo=UTC))
    assert fake.tokens == 2  # a fresh token after the 401


def test_client_errors_do_not_leak_secrets(no_sleep):
    def deny(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(ercot.TOKEN_URL):
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "bad"})
        return httpx.Response(500)

    c = ErcotPublic(
        ErcotCredentials("user", "pw-secret", "subkey-secret"),
        min_request_interval_s=0,
        transport=httpx.MockTransport(deny),
    )
    with pytest.raises(ErcotError) as exc:
        c.archive(
            "np6-346-cd", datetime(2026, 11, 10, tzinfo=UTC), datetime(2026, 11, 13, tzinfo=UTC)
        )
    assert "secret" not in str(exc.value) and "invalid_grant" in str(exc.value)


# ---- Kalshi side of settlement (real settled markets) ----

from pmw.settle import ercot as settle_ercot  # noqa: E402

from .conftest import load_fixture  # noqa: E402

ERCOT_MARKETS = load_fixture("kalshi_settled_KXTXERCOTPEAKD_sample.json")["markets"]


def test_operating_day_is_the_day_before_the_ticker_date():
    days = {m["event_ticker"]: settle_ercot.operating_day(m) for m in ERCOT_MARKETS}
    assert days == {
        "KXTXERCOTPEAKD-26AUG29": date(2026, 8, 28),
        "KXTXERCOTPEAKD-26SEP27": date(2026, 9, 26),
        "KXTXERCOTPEAKD-26NOV15": date(2026, 11, 14),
    }
    bad = dict(ERCOT_MARKETS[0], event_ticker="KXTXERCOTPEAKD-26AUG28")
    with pytest.raises(ValueError, match="ticker implies"):
        settle_ercot.operating_day(bad)


def test_expiration_values_in_both_published_formats():
    assert settle_ercot.parse_mw("81,227.32") == D("81227.32")
    assert settle_ercot.parse_mw("78487.51") == D("78487.51")
    assert settle_ercot.parse_mw("") is None


def test_kalshis_own_values_reproduce_every_result():
    # With Kalshi's recorded expiration value as the peak, strictly-greater must
    # give Kalshi's result for every strike. Checks our reading of the strike.
    peaks = {
        settle_ercot.operating_day(m): settle_ercot.parse_mw(m["expiration_value"])
        for m in ERCOT_MARKETS
    }
    checks = settle_ercot.check_markets(ERCOT_MARKETS, peaks)
    s = settle_ercot.summarize(checks)
    assert s["markets"] == len(ERCOT_MARKETS) and s["reproduced"] == s["markets"]
    assert s["days_value_differs"] == 0
    # Without data, nothing is decided.
    assert settle_ercot.summarize(settle_ercot.check_markets(ERCOT_MARKETS, {}))["no_data"] == len(
        ERCOT_MARKETS
    )


# ---- jobs: backfill is verbatim and resumable ----


def test_backfill_saves_verbatim_and_resumes(tmp_path, no_sleep):
    from pmw.energy import jobs
    from pmw.utils.storage import Storage

    storage = Storage.local(tmp_path)
    fake = FakeErcot()
    c = make_client(fake)
    stats = jobs.backfill(c, storage, "np6-346-cd", date(2026, 11, 1), date(2026, 11, 30))
    assert stats["downloaded"] == 2 and stats["already_had"] == 0
    files = [f.path for f in storage.list("raw/ercot/np6-346-cd")]
    assert "raw/ercot/np6-346-cd/2026/11/101.zip" in files
    assert "raw/ercot/np6-346-cd/manifest/2026-11.parquet" in files
    saved = storage.read_bytes("raw/ercot/np6-346-cd/2026/11/101.zip")
    assert read_doc_csv(saved)[1].startswith(HEADER)  # the document exactly as served
    # A second run lists again but downloads nothing.
    downloads_before = sum(r.method == "POST" and "download" in str(r.url) for r in fake.calls)
    again = jobs.backfill(c, storage, "np6-346-cd", date(2026, 11, 1), date(2026, 11, 30))
    assert again["downloaded"] == 0 and again["already_had"] == 2
    assert (
        sum(r.method == "POST" and "download" in str(r.url) for r in fake.calls) == downloads_before
    )
    manifest = storage.read_parquet("raw/ercot/np6-346-cd/manifest/2026-11.parquet").to_pylist()
    assert [r["doc_id"] for r in manifest] == [101, 102]


def test_backfill_post_hour_filter(tmp_path, no_sleep):
    from pmw.energy import jobs
    from pmw.utils.storage import Storage

    c = make_client(FakeErcot())
    stats = jobs.backfill(
        c, Storage.local(tmp_path), "np6-346-cd", date(2026, 11, 1), date(2026, 11, 30), {6}
    )
    assert stats["listed"] == 2 and stats["kept"] == 0 and stats["downloaded"] == 0


# ---- real documents ----

from .conftest import FIXTURES  # noqa: E402


def test_real_np6_346_reproduces_kalshis_nov14_value():
    name, text = read_doc_csv((FIXTURES / "ercot_np6-346-cd_20261114.zip").read_bytes())
    assert name.endswith("ACTUALSYSLOADFZNP6346.csv")
    rows = parse_load_by_forecast_zone_csv(text)
    day = date(2026, 11, 14)
    assert is_complete(rows, day)
    # KXTXERCOTPEAKD-26NOV15 settled with expiration_value 51346.64.
    assert daily_peak(rows, day) == D("51346.64")
    kalshi = [m for m in ERCOT_MARKETS if m["event_ticker"] == "KXTXERCOTPEAKD-26NOV15"]
    checks = settle_ercot.check_markets(kalshi, {day: daily_peak(rows, day)})
    assert all(c.match and c.value_agrees for c in checks)


def test_real_np3_565_forecast():
    from pmw.energy.ercot import forecast_daily_peaks, parse_load_forecast_csv

    _, text = read_doc_csv((FIXTURES / "ercot_np3-565-cd_20261115T1930.zip").read_bytes())
    rows = parse_load_forecast_csv(text)
    assert len(rows) == 8 * 192
    assert {r.model for r in rows if r.in_use} == {"X"}
    assert rows[0].delivery_day == date(2026, 11, 15) and rows[0].hour_ending == 1
    peaks = forecast_daily_peaks(rows)
    days = sorted({d for d, m in peaks if m == "X"})
    assert days[0] == date(2026, 11, 15) and len(days) == 8
    assert all(D(30000) < v < D(90000) for v in peaks.values())


def test_backfill_then_build_picks_first_complete_version(tmp_path, no_sleep):
    import pyarrow.parquet as pq

    from pmw.energy import jobs
    from pmw.energy.build import build_actuals
    from pmw.utils.storage import Storage

    storage = Storage.local(tmp_path)
    jobs.backfill(
        make_client(FakeErcot()), storage, "np6-346-cd", date(2026, 11, 1), date(2026, 11, 30)
    )
    # Make the earlier-posted document (101) incomplete: 102 must then be chosen.
    partial = day_csv("11/14/2026", hours=[f"{h:02d}:00" for h in range(1, 24)])
    storage.write_bytes("raw/ercot/np6-346-cd/2026/11/101.zip", doc_zip("101.csv", partial))
    stats = build_actuals(tmp_path, tmp_path / "out")
    assert stats["operating_days"] == 1 and stats["days_first_posted_incomplete"] == 1
    (row,) = pq.read_table(tmp_path / "out" / "daily_peak.parquet").to_pylist()
    assert row["first_complete_doc"] == 102 and row["peak_first_complete_str"] == "51346.64"
    assert pq.read_table(tmp_path / "out" / "hourly_load.parquet").num_rows == 24


def test_kalshi_display_precision_is_not_a_disagreement():
    # Real cases from settled markets (ours from NP6-346-CD, Kalshi's recorded text).
    agree = settle_ercot.values_agree
    assert agree(D("73917"), D("73917.77"))  # KXTXERCOTPEAKD-26OCT10, cut to whole MW
    assert agree(D("85799.9"), D("85799.88"))  # KXTXERCOTPEAKD-26SEP12, rounded to 0.1
    assert agree(D("51346.64"), D("51346.64"))
    assert agree(D("73918"), D("73917.77"))  # rounded half-up to whole MW also matches
    assert not agree(D("73916"), D("73917.77"))
    assert not agree(D("51346.65"), D("51346.64"))


def test_in_use_peak_is_built_hour_by_hour():
    from pmw.energy.ercot import ForecastHour, in_use_daily_peaks

    day = date(2026, 11, 14)

    def h(hour: int, model: str, total: str, flag: bool) -> ForecastHour:
        return ForecastHour(day, hour, False, model, flag, D(total))

    rows = []
    for hour in range(1, 25):
        # Model A is flagged before noon, B after; B's own peak (90000) at 08:00 is not in use.
        rows.append(h(hour, "A", "60000" if hour != 18 else "99999", hour <= 12))
        rows.append(h(hour, "B", "90000" if hour == 8 else f"{61000 + hour}", hour > 12))
    assert in_use_daily_peaks(rows) == {day: D("61024")}
    # A missing flag for one hour: the day is not complete.
    assert in_use_daily_peaks([r for r in rows if not (r.hour_ending == 5 and r.in_use)]) == {}
    # Real 2026 posting: one model (X) flagged on every hour.
    from pmw.energy.ercot import forecast_daily_peaks, parse_load_forecast_csv

    _, text = read_doc_csv((FIXTURES / "ercot_np3-565-cd_20261115T1930.zip").read_bytes())
    rows = parse_load_forecast_csv(text)
    inuse = in_use_daily_peaks(rows)
    single = forecast_daily_peaks(rows)
    assert inuse and all(inuse[d] == single[(d, "X")] for d in inuse)


def test_bucket_writes_retry_on_throttling():
    from pmw.energy.jobs import with_retry

    calls, waits = [], []

    def flaky() -> int:
        calls.append(1)
        if len(calls) < 3:
            raise OSError("AWS Error SLOW_DOWN during UploadPart operation")
        return 7

    assert with_retry(flaky, "x", sleep=waits.append) == 7
    assert waits == [2.0, 4.0]
    with pytest.raises(OSError):
        with_retry(lambda: (_ for _ in ()).throw(OSError("down")), "x", sleep=lambda s: None)


def test_real_np3_910_telemetry():
    from pmw.energy.ercot import parse_agg_load_summary

    pts = parse_agg_load_summary((FIXTURES / "ercot_np3-910-er_20261113.zip").read_bytes())
    assert 280 <= len(pts) <= 300  # one SCED run about every 5 minutes
    assert {p.oper_day for p in pts} == {date(2026, 11, 13)}
    first = pts[0]
    assert first.ts_local == datetime(2026, 11, 13, 0, 0, 22)  # noqa: DTZ001 - naive local by design
    assert first.hour_ending == 1
    assert first.system == pytest.approx(45053.4635)
    assert first.zones["Houston"] == pytest.approx(8350.29291)
    # Zones sum close to the system total (DC ties and rounding aside).
    zsum = sum(v for v in first.zones.values() if v is not None)
    assert abs(zsum / first.system - 1) < 0.05
    assert first.ts_utc == datetime(2026, 11, 13, 6, 0, 22, tzinfo=UTC)
