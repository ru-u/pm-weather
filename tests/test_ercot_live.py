"""ercot-live snapshot job, offline: mock HTTP, local storage."""

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from pmw.energy import live
from pmw.utils.storage import Storage

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 17, 4, 35, 12, tzinfo=UTC)


def _dash() -> dict:
    return json.loads((FIX / "ercot_dashboard_supply_demand.json").read_text())


def test_parse_dashboard_marks_projection_and_converts_to_utc():
    pts = live.parse_dashboard(_dash())
    assert len(pts) == 20
    assert pts[0].ts == datetime(2026, 11, 16, 6, 0, tzinfo=UTC)  # 00:00 CST
    assert pts[0].demand_mw == 47334 and not pts[0].projected
    observed = [p for p in pts if not p.projected]
    assert len(observed) == 17
    assert observed[-1].ts == datetime(2026, 11, 17, 4, 30, tzinfo=UTC)  # 22:30 CST
    assert all(p.projected for p in pts[17:])
    assert live.dashboard_last_updated(_dash()) == datetime(2026, 11, 17, 4, 30, tzinfo=UTC)


def test_snapshot_dashboard_saves_verbatim(tmp_path):
    body = _dash()
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    st = Storage.local(tmp_path)
    s = live.snapshot_dashboard(http, st, NOW)
    assert s["observed_points"] == 17 and s["path"].endswith("2026/11/17/043512Z.json.gz")
    saved = json.loads(gzip.decompress(st.read_bytes(s["path"])))
    assert saved["payload"] == body and saved["fetched_utc"] == NOW.isoformat()


def test_nbh_cycles_are_one_and_two_hours_back():
    assert live.nbh_cycles(NOW) == [
        datetime(2026, 11, 17, 3, tzinfo=UTC),
        datetime(2026, 11, 17, 2, tzinfo=UTC),
    ]


def _nbh_file(hour: int) -> bytes:
    """A two-station bulletin built from the saved real block, re-dated to the cycle hour."""
    block = (FIX / "nbh_KIAH_2026110504.txt").read_text()
    utc = " UTC  " + " ".join(f"{(hour + k) % 24:02d}" for k in range(1, 26)) + " "
    lines = block.split("\n")
    lines[0] = lines[0].replace("0400 UTC", f"{hour:02d}00 UTC").replace("11/05/2026", "11/17/2026")
    lines[1] = utc
    kiah = "\n".join(lines)
    return ("\n" + kiah + kiah.replace(" KIAH  ", " KDFW  ", 1)).encode()


def _nbh_transport(calls):
    def handler(req: httpx.Request) -> httpx.Response:
        hour = int(req.url.path.rsplit("t", 1)[-1][:2])
        data = _nbh_file(hour)
        calls.append("range" if "range" in req.headers else "full")
        rng = req.headers.get("range")
        if rng:
            a, b = (int(x) for x in rng.split("=")[1].split("-"))
            part = data[a : b + 1]
            return httpx.Response(
                206, content=part,
                headers={"content-range": f"bytes {a}-{a + len(part) - 1}/{len(data)}"},
            )  # fmt: skip
        return httpx.Response(200, content=data)

    return httpx.MockTransport(handler)


def test_snapshot_nbh_persists_offsets_and_skips_saved_cycles(tmp_path):
    st = Storage.local(tmp_path)
    calls: list[str] = []
    s1 = live.snapshot_nbh(st, NOW, ["KDFW", "KIAH"], _nbh_transport(calls))
    assert len(s1["saved"]) == 2 and s1["full_downloads"] == 2  # one per cycle hour
    rec = [json.loads(x) for x in gzip.decompress(st.read_bytes(s1["saved"][0])).splitlines()]
    assert {r["station"] for r in rec} == {"KDFW", "KIAH"} and all(r["text"] for r in rec)
    # Same run time again: nothing new to fetch.
    s2 = live.snapshot_nbh(st, NOW, ["KDFW", "KIAH"], _nbh_transport(calls))
    assert s2["saved"] == [] and s2["already_had"] == 2
    # The 05:35 run wants 04Z and 03Z. 03Z is saved; 04Z is a cycle hour with no
    # stored offsets yet, so it needs one full download.
    later = NOW.replace(hour=5)
    s3 = live.snapshot_nbh(st, later, ["KDFW", "KIAH"], _nbh_transport(calls))
    assert len(s3["saved"]) == 1 and s3["full_downloads"] == 1
    # Next day, same cycle hours: offsets from the bucket, range requests only.
    nextday = NOW.replace(day=18)
    s4 = live.snapshot_nbh(st, nextday, ["KDFW", "KIAH"], _nbh_transport(calls))
    assert len(s4["saved"]) == 2 and s4["full_downloads"] == 0 and s4["range_requests"] == 4


def test_snapshot_incentives_pages_once_per_day(tmp_path):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(dict(req.url.params))
        if req.url.params.get("cursor"):
            return httpx.Response(200, json={"incentive_programs": [], "next_cursor": ""})
        prog = {"series_ticker": "KXTXERCOTPEAK", "market_ticker": "X"}
        return httpx.Response(200, json={"incentive_programs": [prog], "next_cursor": "c1"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    st = Storage.local(tmp_path)
    s = live.snapshot_incentives(http, st, NOW)
    assert s["programs"] == 2 and s["ercot_or_snow_series"] == ["KXTXERCOTPEAK"]
    assert [p["status"] for p in seen] == ["active", "active", "upcoming", "upcoming"]
    assert live.snapshot_incentives(http, st, NOW)["already_had"] is True
    assert len(seen) == 4


def test_run_once_isolates_failures_and_skips_archives_without_credentials(tmp_path):
    def handler(req: httpx.Request) -> httpx.Response:
        if "ercot.com" in req.url.host:
            return httpx.Response(503)
        return httpx.Response(200, json={"incentive_programs": [], "next_cursor": ""})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    st = Storage.local(tmp_path)
    s = live.run_once(st, NOW, ["KIAH"], None, http=http, nbh_transport=_nbh_transport([]))
    assert "error" in s["dashboard"]
    assert s["incentives"]["programs"] == 0
    assert len(s["nbh"]["saved"]) == 2
    assert s["archives"] == {"skipped": "no ERCOT credentials on this service"}
    assert s["ok"] is False
    assert st.exists("live/ercot/runs/2026/11/17/043512Z.json")
