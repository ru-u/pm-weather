"""Daily summary and gap detection over logger output. Needs only pyarrow."""

from __future__ import annotations

import itertools
import logging
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from pmw.logger.schemas import TABLES
from pmw.utils.storage import Storage

log = logging.getLogger(__name__)


def _read_day(storage: Storage, table: str, day: date, columns: list[str]) -> pa.Table | None:
    source, _schema = TABLES[table]
    files = storage.list(f"live/{source}/{table}/date={day.isoformat()}")
    parts = []
    for f in files:
        if f.path.endswith(".parquet"):
            parts.append(storage.read_parquet(f.path).select(columns))
    if not parts:
        return None
    return pa.concat_tables(parts)


def find_gaps(
    times: list[datetime], expected_s: int, tolerance: float = 1.6
) -> list[tuple[datetime, datetime, float]]:
    """Intervals between consecutive snapshots longer than tolerance * expected."""
    ordered = sorted(times)
    gaps = []
    for a, b in itertools.pairwise(ordered):
        delta = (b - a).total_seconds()
        if delta > expected_s * tolerance:
            gaps.append((a, b, delta))
    return gaps


def storage_usage(storage: Storage) -> dict[str, tuple[int, int]]:
    """(file count, bytes) per top-level area, e.g. live/kalshi/orderbooks."""
    usage: dict[str, list[int]] = {}
    for f in storage.list(""):
        parts = f.path.split("/")
        key = "/".join(parts[:3]) if parts[0] in ("live", "raw") else parts[0]
        entry = usage.setdefault(key, [0, 0])
        entry[0] += 1
        entry[1] += f.size
    return {k: (v[0], v[1]) for k, v in sorted(usage.items())}


def build_report(storage: Storage, day: date, base_interval_s: int = 300) -> dict[str, Any]:
    out: dict[str, Any] = {"date": day.isoformat(), "storage": storage.describe()}

    ob = _read_day(storage, "orderbooks", day, ["received_at_utc", "ticker", "grp", "trigger"])
    if ob is None:
        out["orderbooks"] = None
    else:
        base = ob.filter(pc.equal(ob["trigger"], "base"))
        # One base cycle = one batch time; bucket to the minute to merge batches.
        cycle_times = sorted(
            {t.replace(second=0, microsecond=0) for t in base["received_at_utc"].to_pylist()}
        )
        gaps = find_gaps(cycle_times, base_interval_s)
        expected = int(86400 / base_interval_s)
        out["orderbooks"] = {
            "snapshots": ob.num_rows,
            "markets": pc.count_distinct(ob["ticker"]).as_py(),
            "by_group": dict(Counter(ob["grp"].to_pylist())),
            "by_trigger": dict(Counter(ob["trigger"].to_pylist())),
            "base_cycles": len(cycle_times),
            "base_cycles_expected_full_day": expected,
            "first": cycle_times[0].isoformat() if cycle_times else None,
            "last": cycle_times[-1].isoformat() if cycle_times else None,
            "gaps": [
                {"from": a.isoformat(), "to": b.isoformat(), "minutes": round(s / 60, 1)}
                for a, b, s in gaps
            ],
        }

    tr = _read_day(storage, "trades", day, ["trade_id", "series_ticker", "count", "yes_price"])
    if tr is None:
        out["trades"] = None
    else:
        seen: set[str] = set()
        contracts: Counter[str] = Counter()
        dollars: Counter[str] = Counter()
        for tid, series, count, px in zip(
            tr["trade_id"].to_pylist(),
            tr["series_ticker"].to_pylist(),
            tr["count"].to_pylist(),
            tr["yes_price"].to_pylist(),
            strict=True,
        ):
            if tid in seen:
                continue
            seen.add(tid)
            contracts[series] += count or 0
            dollars[series] += (count or 0) * (px or 0)
        out["trades"] = {
            "unique_trades": len(seen),
            "contracts_by_series": {k: round(v, 2) for k, v in contracts.most_common()},
            "yes_notional_by_series": {k: round(v, 2) for k, v in dollars.most_common()},
        }

    he = _read_day(storage, "health", day, ["kind", "n_err", "rss_mb", "duration_s"])
    if he is not None:
        errs: Counter[str] = Counter()
        for kind, n_err in zip(he["kind"].to_pylist(), he["n_err"].to_pylist(), strict=True):
            if n_err:
                errs[kind] += 1
        rss = [x for x in he["rss_mb"].to_pylist() if x is not None]
        out["health"] = {
            "stages_with_errors": dict(errs),
            "rss_mb_min": min(rss) if rss else None,
            "rss_mb_max": max(rss) if rss else None,
        }

    sm_today = _read_day(storage, "series_meta", day, ["series_ticker", "title", "grp"])
    if sm_today is not None:
        earlier: set[str] = set()
        prefix = "live/kalshi/series_meta/"
        for f in storage.list(prefix):
            d = f.path[len(prefix) :].split("/")[0].removeprefix("date=")
            if d < day.isoformat():
                earlier.update(storage.read_parquet(f.path)["series_ticker"].to_pylist())
        today = dict(
            zip(sm_today["series_ticker"].to_pylist(), sm_today["title"].to_pylist(), strict=True)
        )
        out["series_seen"] = len(today)
        out["new_series"] = (
            {k: v for k, v in sorted(today.items()) if k not in earlier} if earlier else {}
        )

    out["usage"] = {k: {"files": n, "bytes": b} for k, (n, b) in storage_usage(storage).items()}
    return out


def format_report(r: dict[str, Any]) -> str:
    lines = [f"# pmw logger report for {r['date']}", f"storage: {r['storage']}", ""]
    ob = r.get("orderbooks")
    if not ob:
        lines.append("ORDERBOOKS: none logged for this date")
    else:
        lines += [
            f"ORDERBOOKS: {ob['snapshots']:,} snapshots across {ob['markets']} markets",
            f"  base cycles: {ob['base_cycles']} (a full day is "
            f"{ob['base_cycles_expected_full_day']}), {ob['first']} to {ob['last']}",
            f"  by group:   {ob['by_group']}",
            f"  by trigger: {ob['by_trigger']}",
            f"  gaps: {len(ob['gaps'])}",
        ]
        lines += [f"    {g['from']} -> {g['to']} ({g['minutes']} min)" for g in ob["gaps"][:20]]
    tr = r.get("trades")
    lines.append("")
    if not tr:
        lines.append("TRADES: none logged for this date")
    else:
        lines.append(f"TRADES: {tr['unique_trades']:,} unique prints")
        for series, n in list(tr["contracts_by_series"].items())[:25]:
            usd = tr["yes_notional_by_series"].get(series, 0)
            lines.append(f"  {series:<24} {n:>12,.0f} contracts  ${usd:>10,.0f} YES notional")
    he = r.get("health")
    if he:
        lines += [
            "",
            f"HEALTH: stages with errors {he['stages_with_errors'] or 'none'}; "
            f"RSS {he['rss_mb_min']} to {he['rss_mb_max']} MB",
        ]
    if "new_series" in r:
        lines += [
            "",
            f"SERIES: {r['series_seen']} recorded today; new: {r['new_series'] or 'none'}",
        ]
    lines += ["", "STORAGE (cumulative):"]
    total = 0
    for key, u in r["usage"].items():
        total += u["bytes"]
        lines.append(f"  {key:<32} {u['files']:>6} files {u['bytes'] / 1e6:>9.2f} MB")
    lines.append(f"  {'TOTAL':<32} {'':>12} {total / 1e6:>9.2f} MB")
    return "\n".join(lines)


def prune_raw(storage: Storage, retention_days: int, today: date) -> int:
    """Delete raw API responses older than the retention window. Returns files deleted."""
    cutoff = (today - timedelta(days=retention_days)).isoformat()
    n = 0
    for f in storage.list("raw"):
        marker = [p for p in f.path.split("/") if p.startswith("date=")]
        if marker and marker[0].removeprefix("date=") < cutoff:
            storage.delete(f.path)
            n += 1
    return n
