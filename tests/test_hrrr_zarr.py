"""HRRR Zarr point reader, offline (a fake store with real blosc encoding)."""

import json
from datetime import UTC, datetime

import httpx
import numcodecs
import numpy as np
import pytest

from pmw.weather import hrrr_zarr as hz


def test_grid_point_matches_archive_coordinates():
    # Checked against the archive's projection_x/y arrays on 2026-11-17.
    assert hz.grid_point(40.7789, -73.9692) == hz.GridPoint(698, 1553)  # Central Park
    assert hz.grid_point(40.7789, -73.9692).tile == (4, 10)
    with pytest.raises(ValueError):
        hz.grid_point(10.0, -150.0)


def test_run_for_requires_the_run_complete_strictly_before_t():
    assert hz.run_for(datetime(2026, 1, 25, 12, tzinfo=UTC)) == datetime(2026, 1, 25, 6, tzinfo=UTC)
    assert hz.run_for(datetime(2026, 1, 25, 8, tzinfo=UTC)) == datetime(2026, 1, 25, 0, tzinfo=UTC)
    assert hz.run_for(datetime(2026, 1, 25, 8, 1, tzinfo=UTC)) == datetime(
        2026, 1, 25, 6, tzinfo=UTC
    )


def _store(hours: np.ndarray, block: np.ndarray) -> httpx.MockTransport:
    blosc = numcodecs.Blosc(cname="lz4", clevel=5, shuffle=1)
    meta = {
        "metadata": {
            "surface/ASNOW_acc_fcst/.zarray": {
                "chunks": list(block.shape), "shape": [len(hours), 1059, 1799],
                "dtype": "<f2", "fill_value": -10000.0,
            },
            "surface/ASNOW_acc_fcst/.zattrs": {
                "_ARRAY_DIMENSIONS": ["time_2", "projection_y_coordinate", "projection_x_coordinate"]
            },
            "forecast_period_2/.zarray": {"chunks": [len(hours)], "shape": [len(hours)], "dtype": "<i8"},
        }
    }  # fmt: skip

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/.zmetadata"):
            return httpx.Response(200, text=json.dumps(meta))
        if path.endswith("/forecast_period_2/0"):
            return httpx.Response(200, content=blosc.encode(hours.astype("<i8").tobytes()))
        if path.endswith("/0.4.10"):
            return httpx.Response(200, content=blosc.encode(block.astype("<f2").tobytes()))
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_point_series_reads_the_right_cell_and_masks_fill():
    hours = np.arange(2, 7)
    block = np.zeros((5, 150, 150), dtype="<f2")
    block[:, 698 % 150, 1553 % 150] = [0.0, 0.01, 0.05, -10000.0, 0.2]
    h = hz.HrrrZarr(workers=2, transport=_store(hours, block))
    out = h.point_series(
        datetime(2026, 1, 25, tzinfo=UTC), "asnow_m",
        {"KNYC": hz.GridPoint(698, 1553), "FAR": hz.GridPoint(10, 10)},
    )  # fmt: skip
    assert out is not None and set(out) == {"KNYC"}  # FAR's tile is absent (404)
    hrs, vals = out["KNYC"]
    assert hrs.tolist() == [2, 3, 4, 5, 6]
    assert np.isnan(vals[3]) and vals[4] == pytest.approx(0.2, abs=1e-3)
