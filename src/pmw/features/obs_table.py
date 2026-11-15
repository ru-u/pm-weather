"""Observed daily snowfall at settlement stations, as settlement uses it.

The NWS Daily Climate Report value (via IEM) where it exists, otherwise ACIS
(the same source the settlement replication uses for gaps). Both serve the
latest version of a day's value; see docs/settlement_rules.md.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from pmw.weather.iem_cli import IemCli

SCHEMA = pa.schema(
    [
        ("station", pa.string()),
        ("date", pa.date32()),
        ("snow_in", pa.float32()),  # trace counts as 0.0; null if missing everywhere
        ("trace", pa.bool_()),
        ("source", pa.string()),  # "cli" | "acis" | null
    ]
)


def build_observed(
    stations: list[str], years: list[int], climo_path: Path, out: Path
) -> dict[str, int]:
    acis = {
        (r["station"], r["date"]): r
        for r in pq.read_table(climo_path).to_pylist()
        if r["station"] in stations and r["date"].year in years
    }
    iem = IemCli()
    rows = []
    stats = {"cli": 0, "acis": 0, "missing": 0}
    for st in stations:
        cli = {d.valid: d for y in years for d in iem.year(st, y)}
        for (s, day), a in sorted(acis.items()):
            if s != st:
                continue
            c = cli.get(day)
            if c is not None and not c.snow.missing:
                rows.append({"station": st, "date": day, "snow_in": float(c.snow.measurable),
                             "trace": c.snow.trace, "source": "cli"})  # fmt: skip
                stats["cli"] += 1
            elif not a["missing"]:
                rows.append({"station": st, "date": day, "snow_in": a["snow_in"],
                             "trace": a["trace"], "source": "acis"})  # fmt: skip
                stats["acis"] += 1
            else:
                rows.append(
                    {"station": st, "date": day, "snow_in": None, "trace": False, "source": None}
                )
                stats["missing"] += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), out, compression="zstd")
    return stats
