"""NBM text bulletins: parser on real station blocks, fetcher against a fake S3."""

import itertools
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from pmw.weather.nbm_text import (
    NbmTextArchive,
    available_at,
    bulletin_url,
    parse_nbp_block,
    pull_day,
    read_day,
    split_blocks,
)

FIX = Path(__file__).parent / "fixtures"


def _text(name: str) -> str:
    return (FIX / name).read_text()


def test_parse_storm_block_units_and_periods():
    # 01Z cycle on 2026-01-25, hours before the storm that left 11.4 in at Central Park.
    b = parse_nbp_block(_text("nbp_KNYC_2026012501.txt"))
    assert (b.station, b.version) == ("KNYC", "V4.3")
    assert b.issue == datetime(2026, 1, 25, 1, 0, tzinfo=UTC)
    first = b.periods[0]
    assert first.fhr == 35 and first.valid_end == datetime(2026, 1, 26, 12, 0, tzinfo=UTC)
    # Tenths of an inch in the bulletin: 80/110/132/178/195.
    assert first.snow_in == {10: 8.0, 25: 11.0, 50: 13.2, 75: 17.8, 90: 19.5}
    assert first.qpf_in[50] == 1.58  # hundredths of an inch
    assert len(b.periods) == 9
    assert all(p.valid_end.hour == 12 for p in b.periods)  # every 24-hour period ends at 12Z
    assert [p.fhr for p in b.periods] == [35, 59, 83, 107, 131, 155, 179, 203, 227]


def test_percentiles_are_monotone_in_real_blocks():
    for name in ("nbp_KNYC_2026012501.txt", "nbp_KBOS_2026012501.txt", "nbp_KNYC_2021120113.txt"):
        for p in parse_nbp_block(_text(name)).periods:
            vals = [p.snow_in[q] for q in (10, 25, 50, 75, 90)]
            assert all(v is not None for v in vals)
            assert vals == sorted(vals), (name, p.fhr)


def test_parse_older_version_with_shifted_columns():
    # V4.0, 13Z cycle: the first day group has one column and no 24-hour values.
    b = parse_nbp_block(_text("nbp_KNYC_2021120113.txt"))
    assert (b.station, b.version) == ("KNYC", "V4.0")
    assert b.fhrs[:3] == [23, 35, 47]
    assert b.periods[0].fhr == 47
    assert b.periods[0].valid_end == datetime(2021, 12, 3, 12, 0, tzinfo=UTC)
    assert all(p.valid_end.hour == 12 for p in b.periods)
    by_fhr = {p.fhr: p for p in b.periods}
    assert by_fhr[191].snow_in[90] == 0.8 and by_fhr[191].snow_in[75] == 0.2
    assert by_fhr[215].snow_in[90] == 1.4


def test_parse_rejects_other_products_and_garbage():
    with pytest.raises(ValueError):
        parse_nbp_block("not a block\n")
    nbs = _text("nbp_KNYC_2026012501.txt").replace("NBP GUIDANCE", "NBS GUIDANCE")
    with pytest.raises(ValueError):
        parse_nbp_block(nbs)


def test_available_at():
    cycle = datetime(2026, 1, 25, 13, 0, tzinfo=UTC)
    on_time = cycle + timedelta(minutes=68)
    assert available_at(cycle, on_time) == on_time
    # Uploaded 15 hours late in a batch: says nothing about real-time availability.
    assert available_at(cycle, cycle + timedelta(hours=15)) == cycle + timedelta(hours=2)
    assert available_at(cycle, None) == cycle + timedelta(hours=2)
    assert available_at(cycle, cycle - timedelta(minutes=5)) == cycle + timedelta(hours=2)


def test_bulletin_url():
    assert bulletin_url("nbp", datetime(2026, 1, 25, 1, tzinfo=UTC)) == (
        "https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.20260125/01/text/blend_nbptx.t01z"
    )


class FakeS3:
    """Serves one bulletin body for every cycle; `shift` simulates a changed station list."""

    def __init__(self) -> None:
        self.body = (FIX / "nbp_mini_bulletin_2026012501.txt").read_bytes()
        self.requests: list[str] = []
        self.missing_hours: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        rng = request.headers.get("range")
        self.requests.append("range" if rng else "full")
        if any(f"/{h}/text/" in str(request.url) for h in self.missing_hours):
            return httpx.Response(404)
        headers = {"last-modified": "Sun, 25 Jan 2026 02:09:00 GMT"}
        if rng:
            start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
            return httpx.Response(206, content=self.body[start : end + 1], headers=headers)
        return httpx.Response(200, content=self.body, headers=headers)


def _archive(fake: FakeS3) -> NbmTextArchive:
    return NbmTextArchive("nbp", workers=2, transport=httpx.MockTransport(fake))


def test_fetcher_learns_offsets_once_then_uses_range_requests():
    fake = FakeS3()
    arc = _archive(fake)
    c1 = datetime(2026, 1, 25, 1, tzinfo=UTC)
    first = arc.fetch_cycle(c1, ["KNYC", "KBOS"])
    assert fake.requests == ["full"] and [b.how for b in first] == ["full", "full"]
    assert parse_nbp_block(first[0].text).station == "KNYC"
    fake.requests.clear()
    second = arc.fetch_cycle(c1 + timedelta(days=1), ["KNYC", "KBOS"])
    assert sorted(fake.requests) == ["range", "range"]
    assert [b.how for b in second] == ["range", "range"]
    assert second[0].text == first[0].text and second[1].text == first[1].text
    assert second[0].last_modified == datetime(2026, 1, 25, 2, 9, tzinfo=UTC)


def test_fetcher_recovers_when_offsets_move():
    fake = FakeS3()
    arc = _archive(fake)
    c1 = datetime(2026, 1, 25, 1, tzinfo=UTC)
    arc.fetch_cycle(c1, ["KNYC", "KBOS"])
    fake.body = (b" " * 80 + b"\n") * 120 + fake.body  # station list changed: blocks move 9.7 KB
    fake.requests.clear()
    blocks = arc.fetch_cycle(c1 + timedelta(days=1), ["KNYC", "KBOS"])
    assert "full" in fake.requests
    assert all(b.text is not None for b in blocks)
    assert parse_nbp_block(blocks[0].text).station == "KNYC"
    assert parse_nbp_block(blocks[1].text).station == "KBOS"


def test_missing_cycle_and_missing_station_are_recorded_not_fatal():
    fake = FakeS3()
    arc = _archive(fake)
    c1 = datetime(2026, 1, 25, 1, tzinfo=UTC)
    blocks = arc.fetch_cycle(c1, ["KNYC", "KZZZ"])
    assert blocks[0].text is not None and blocks[1].text is None and blocks[1].how == "missing"
    fake.missing_hours = {"07"}
    gone = arc.fetch_cycle(c1.replace(hour=7), ["KNYC"])
    assert gone[0].text is None and gone[0].how == "missing"


def test_pull_day_is_resumable_and_round_trips(tmp_path):
    fake = FakeS3()
    arc = _archive(fake)
    day = date(2026, 1, 25)
    blocks, missing = pull_day(arc, tmp_path, day, ["KNYC", "KBOS"], [0, 6])
    assert (blocks, missing) == (4, 0)  # offsets are remembered per cycle hour
    rows = list(read_day(tmp_path, "nbp", day))
    assert len(rows) == 4 and {r["station"] for r in rows} == {"KNYC", "KBOS"}
    assert parse_nbp_block(rows[0]["text"]).periods
    n = len(fake.requests)
    assert pull_day(arc, tmp_path, day, ["KNYC", "KBOS"], [0, 6]) == (0, 0)  # already done
    assert len(fake.requests) == n


def test_split_blocks():
    blocks = split_blocks((FIX / "nbp_mini_bulletin_2026012501.txt").read_bytes())
    assert list(blocks) == ["KBOS", "KDCA", "KNYC"]
    assert blocks["KBOS"][0] == 0 and all(n == 3807 for _, n in blocks.values())


def test_table_rows_carry_availability_and_units(tmp_path):
    from pmw.features.nbm_table import SCHEMA, build_table

    fake = FakeS3()
    arc = _archive(fake)
    day = date(2026, 1, 25)
    pull_day(arc, tmp_path, day, ["KNYC", "KBOS"], [1])
    out = tmp_path / "t.parquet"
    stats = build_table(tmp_path, [day, date(2026, 1, 26)], out)
    assert stats == {"days_present": 1, "days_absent": 1, "rows": 18, "failed_blocks": 0}
    import pyarrow.parquet as pq

    table = pq.read_table(out)
    assert table.schema.equals(SCHEMA)
    rows = [r for r in table.to_pylist() if r["station"] == "KNYC"]
    first = min(rows, key=lambda r: r["fhr"])
    assert abs(first["snow_p50"] - 13.2) < 1e-5 and abs(first["qpf_p50"] - 1.58) < 1e-5
    # Fake S3 says the file was uploaded 02:09, 69 minutes after the 01Z cycle.
    assert first["available_at"] == datetime(2026, 1, 25, 2, 9, tzinfo=UTC)
    assert all(r["available_at"] > r["cycle"] for r in rows)
    assert first["nbm_version"] == "V4.3"


def test_parse_nbs_six_hour_snow():
    from pmw.weather.nbm_text import parse_nbs_block

    b = parse_nbs_block(_text("nbs_KNYC_2026012401.txt"))
    assert (b.station, b.version) == ("KNYC", "V4.3")
    assert b.issue == datetime(2026, 1, 24, 1, tzinfo=UTC)
    ends = sorted(b.s06_in)
    assert len(ends) == 11 and all(e.hour in (0, 6, 12, 18) for e in ends)
    assert ends[0] == datetime(2026, 1, 24, 12, tzinfo=UTC)  # fhr 11
    assert all((y - x).total_seconds() == 6 * 3600 for x, y in itertools.pairwise(ends))
    # Row: 0 0 0 0 1 46 46 15 6 5 1 (tenths), for periods ending 12Z Jan 24 ... 12Z Jan 26 + 12h.
    by_end = {e.strftime("%d %HZ"): v for e, v in b.s06_in.items()}
    assert by_end["25 12Z"] == 0.1
    assert by_end["25 18Z"] == 4.6 and by_end["26 00Z"] == 4.6  # the storm: 12Z Jan 25 to 00Z
    assert by_end["26 06Z"] == 1.5 and by_end["27 00Z"] == 0.1


def test_window_search_tracks_drifting_offsets():
    fake = FakeS3()
    arc = NbmTextArchive("nbs", workers=2, transport=httpx.MockTransport(fake))
    c1 = datetime(2026, 1, 25, 1, tzinfo=UTC)
    first = arc.fetch_cycle(c1, ["KNYC", "KBOS"])
    fake.body = b"x" * 79 + b"\n" + b" " * 79 + b"\n" + fake.body  # blocks drift by 160 bytes
    fake.requests.clear()
    second = arc.fetch_cycle(c1 + timedelta(days=1), ["KNYC", "KBOS"])
    assert fake.requests == ["range", "range"]  # found inside the window, no full download
    assert [b.text for b in second] == [b.text for b in first]
    assert arc.offsets[(1, "KBOS")][0] == 160  # the drift is remembered


# ---------- NBH (hourly) ----------


def test_parse_nbh_hourly_temperature_and_spread():
    from pmw.weather.nbm_text import parse_nbh_block

    b = parse_nbh_block(_text("nbh_KIAH_2026070112.txt"))
    assert (b.station, b.version) == ("KIAH", "V5.0")
    assert b.issue == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    tmp = b.values["TMP"]
    # Columns run issue + 1 h to issue + 25 h.
    assert min(tmp) == datetime(2026, 7, 1, 13, tzinfo=UTC)
    assert max(tmp) == datetime(2026, 7, 2, 13, tzinfo=UTC)
    assert len(tmp) == 25
    # " TMP  81 84 86 88 91 92 93 94 94 ..." and " TSD   1  1  1  1  2  2  2  3  3 ..."
    assert [tmp[datetime(2026, 7, 1, h, tzinfo=UTC)] for h in (13, 14, 20, 21)] == [81, 84, 94, 94]
    assert b.values["TSD"][datetime(2026, 7, 1, 20, tzinfo=UTC)] == 3
    assert b.values["DPT"][datetime(2026, 7, 2, 13, tzinfo=UTC)] == 76


def test_parse_nbh_old_version_without_spread():
    from pmw.weather.nbm_text import parse_nbh_block

    b = parse_nbh_block(_text("nbh_KDFW_2020060112.txt"))
    assert (b.station, b.version) == ("KDFW", "V3.2")
    assert "TSD" not in b.values
    assert b.values["TMP"][datetime(2020, 6, 1, 20, tzinfo=UTC)] == 86
    assert b.values["TMP"][datetime(2020, 6, 2, 13, tzinfo=UTC)] == 73


def test_parse_nbh_negative_and_three_digit_fields():
    from pmw.weather.nbm_text import parse_nbh_block

    text = _text("nbh_KDFW_2020060112.txt")
    lines = text.split("\n")
    i = next(k for k, ln in enumerate(lines) if ln.startswith(" TMP"))
    # Fields are 3 characters wide and can touch: "-10" and "103" fill them.
    lines[i] = " TMP -10 -5  0" + "103" * 22
    b = parse_nbh_block("\n".join(lines))
    tmp = b.values["TMP"]
    assert [tmp[datetime(2020, 6, 1, h, tzinfo=UTC)] for h in (13, 14, 15, 16)] == [-10, -5, 0, 103]


def test_parse_nbh_rejects_misaligned_hours():
    from pmw.weather.nbm_text import parse_nbh_block

    text = _text("nbh_KDFW_2020060112.txt").replace(" UTC  13 14", " UTC  14 15", 1)
    with pytest.raises(ValueError):
        parse_nbh_block(text)
