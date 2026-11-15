"""Command line entry point: `pmw <command>`.

None of these commands loads Kalshi trading credentials. `pmw log` and
`pmw discover` talk only to public endpoints.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from decimal import Decimal

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command()
def discover(
    all_categories: bool = typer.Option(
        False, help="Scan all ~16k series, not just our categories."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """List every open Kalshi market matching the filters in configs/series.yaml."""
    from pmw.logger.discovery import discover as run_discovery
    from pmw.logger.discovery import format_table
    from pmw.markets.kalshi import KalshiPublic
    from pmw.utils.config import load_logger_config, load_series_config

    _setup_logging()
    cfg = load_logger_config()
    client = KalshiPublic(
        cfg.kalshi.base_url,
        min_request_interval_s=cfg.kalshi.min_request_interval_s,
        user_agent=cfg.kalshi.user_agent,
    )
    result = run_discovery(client, load_series_config().kalshi, all_categories=all_categories)
    client.close()
    tradable = result.tradable()
    if as_json:
        typer.echo(
            json.dumps(
                [
                    {
                        "ticker": m.ticker,
                        "event": m.event_ticker,
                        "series": m.series_ticker,
                        "group": m.grp,
                        "title": m.title,
                        "strike": m.yes_sub_title,
                        "close_time": m.close_time.isoformat() if m.close_time else None,
                        "yes_bid": m.raw.get("yes_bid_dollars"),
                        "yes_ask": m.raw.get("yes_ask_dollars"),
                        "volume": m.raw.get("volume_fp"),
                        "open_interest": m.raw.get("open_interest_fp"),
                    }
                    for m in tradable
                ],
                indent=1,
            )
        )
        return
    typer.echo(
        f"scanned {result.n_series_scanned} series, matched {len(result.series)}, "
        f"{len(tradable)} open markets, {client.request_count} requests, "
        f"{len(result.errors)} errors"
    )
    by_group: dict[str, int] = {}
    for g in result.series_group.values():
        by_group[g] = by_group.get(g, 0) + 1
    typer.echo(f"matched series by group: {by_group}")
    typer.echo(format_table(result))
    for err in result.errors:
        typer.echo(f"ERROR {err}", err=True)


@app.command()
def log(
    cycles: int = typer.Option(0, help="Stop after this many base cycles (0 = run forever)."),
    base_s: int = typer.Option(0, help="Override the base cadence in seconds (for local tests)."),
    hot_s: int = typer.Option(0, help="Override the hot cadence in seconds (for local tests)."),
    no_align: bool = typer.Option(False, help="Start the first base cycle immediately."),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Run the market logger (orderbooks, market snapshots, trades) until stopped."""
    from pmw.logger.runner import MarketLogger
    from pmw.utils.config import load_logger_config, load_series_config
    from pmw.utils.storage import Storage

    _setup_logging(verbose)
    cfg = load_logger_config()
    if base_s:
        cfg.cadence.orderbook_base_s = base_s
    if hot_s:
        cfg.cadence.orderbook_hot_s = hot_s
    logger = MarketLogger(cfg, load_series_config(), Storage.from_env())
    logger.run(
        max_base_cycles=cycles or None,
        align=not no_align,
        drain_on_exit_s=120.0 if cycles else 0.0,
    )
    typer.echo(
        f"done: {logger.base_cycles} base cycles, {logger.hot_cycles} hot cycles, "
        f"{logger.client.request_count} requests, rows {logger.buffers.rows_written}, "
        f"bytes {logger.buffers.bytes_written}, raw bytes {logger.raw.bytes_written}"
    )


@app.command()
def report(
    day: str = typer.Option("", "--date", help="UTC date YYYY-MM-DD (default: yesterday)."),
    today: bool = typer.Option(False, help="Report on today so far instead of yesterday."),
    save: bool = typer.Option(True, help="Also write the report into storage under reports/."),
    prune: bool = typer.Option(True, help="Delete raw responses past the retention window."),
) -> None:
    """Daily summary: markets tracked, snapshots, gaps, volume by series, new series."""
    from pmw.logger.report import build_report, format_report, prune_raw
    from pmw.logger.runner import send_alert
    from pmw.utils.config import load_logger_config
    from pmw.utils.storage import Storage
    from pmw.utils.timeutil import utcnow

    _setup_logging()
    cfg = load_logger_config()
    storage = Storage.from_env()
    now = utcnow().date()
    target = date.fromisoformat(day) if day else (now if today else now - timedelta(days=1))
    r = build_report(storage, target, cfg.cadence.orderbook_base_s)
    text = format_report(r)
    typer.echo(text)
    if save:
        storage.write_bytes(f"reports/{target.isoformat()}.md", text.encode())
        storage.write_bytes(f"reports/{target.isoformat()}.json", json.dumps(r, indent=1).encode())
    ob = r.get("orderbooks")
    if target < now and (not ob or ob["gaps"] or ob["base_cycles"] < 0.95 * 86400 / 300):
        n_gaps = len(ob["gaps"]) if ob else "n/a"
        n_cycles = ob["base_cycles"] if ob else 0
        send_alert(f"pmw logger {target}: {n_cycles} base cycles, {n_gaps} gaps")
    if prune:
        n = prune_raw(storage, cfg.raw.retention_days, now)
        if n:
            typer.echo(f"pruned {n} raw files older than {cfg.raw.retention_days} days")


@app.command()
def fee(
    price: float = typer.Argument(..., help="Price in dollars of the side you buy, e.g. 0.59"),
    count: float = typer.Option(1.0, help="Contracts"),
    maker: bool = typer.Option(False, help="Resting (maker) fill instead of taker"),
    fee_type: str = typer.Option("quadratic"),
    multiplier: float = typer.Option(1.0),
) -> None:
    """Kalshi fee for one fill, using the documented model and rounding."""
    from pmw.markets.fees import fill_fee

    f = fill_fee(
        Decimal(str(price)),
        Decimal(str(count)),
        fee_type=fee_type,
        fee_multiplier=Decimal(str(multiplier)),
        is_taker=not maker,
    )
    typer.echo(
        f"trade fee ${f.trade_fee}  rounding ${f.rounding_fee}  total ${f.total}  "
        f"(${f.total / Decimal(str(count)):.6f} per contract)"
    )


SNOW_SERIES_WITH_HISTORY = [
    "KXNYCSNOWM", "KXBOSSNOWM", "KXDCSNOWM", "KXPHILSNOWM", "KXCHISNOWM", "KXDETSNOWM",
    "KXSEASNOWM", "KXSLCSNOWM", "KXDALSNOWM", "KXAUSSNOWM", "KXHOUSNOWM", "KXSNOWNYM",
    "KXSNOWSTORM",
]  # fmt: skip


@app.command("settle-check")
def settle_check(
    series: str = typer.Argument("KXNYCSNOWM", help="Snow series ticker, or 'all'"),
    brief: bool = typer.Option(False, help="Only print events and mismatches."),
    no_fallback: bool = typer.Option(False, help="Use CLI data only; do not fill gaps from ACIS."),
) -> None:
    """Replicate settlement of settled snow markets from NWS daily snowfall data."""
    from pmw.markets.kalshi import KalshiPublic
    from pmw.settle.snow import check_series, format_checks, summarize
    from pmw.utils.config import load_logger_config, load_stations_config
    from pmw.weather.acis import Acis
    from pmw.weather.iem_cli import IemCli

    _setup_logging()
    stations = load_stations_config()
    client = KalshiPublic(load_logger_config().kalshi.base_url, min_request_interval_s=0.5)
    iem, acis = IemCli(), (None if no_fallback else Acis())
    targets = SNOW_SERIES_WITH_HISTORY if series == "all" else [series]
    totals: dict[str, int] = {}
    for target in targets:
        checks = check_series(client, iem, target, stations, acis)
        typer.echo(format_checks(checks, target, verbose=not brief and series != "all"))
        for key, value in summarize(checks).items():
            totals[key] = totals.get(key, 0) + value
    client.close()
    if len(targets) > 1:
        typer.echo(f"\nALL SERIES: {totals}")
    if totals["reproduced"] != totals["markets"]:
        raise typer.Exit(1)


@app.command("nbm-pull")
def nbm_pull(
    start: str = typer.Argument(..., help="First UTC date, YYYY-MM-DD"),
    end: str = typer.Argument(..., help="Last UTC date, YYYY-MM-DD"),
    product: str = typer.Option("nbp", help="nbp (probabilistic) or nbs (short range)"),
    hours: str = typer.Option(
        "1,7,13,19", help="Cycle hours (UTC). Only 01/07/13/19Z carry the 24-hour percentiles."
    ),
    out: str = typer.Option("data/raw/nbm", help="Output root"),
) -> None:
    """Download archived NBM text blocks for the configured stations (resumable per day)."""
    from datetime import date as _date
    from pathlib import Path

    from pmw.utils.config import load_stations_config
    from pmw.utils.timeutil import date_range
    from pmw.weather.nbm_text import NbmTextArchive, pull_day

    _setup_logging()
    stations = sorted(load_stations_config().stations)
    hour_list = [int(h) for h in hours.split(",")]
    archive = NbmTextArchive(product)
    root = Path(out)
    total = missing = 0
    days = date_range(_date.fromisoformat(start), _date.fromisoformat(end))
    for done, day in enumerate(days, 1):
        b, m = pull_day(archive, root, day, stations, hour_list)
        total += b
        missing += m
        if done % 10 == 0 or done == len(days):
            typer.echo(
                f"{day}  {done}/{len(days)} days  blocks={total} missing={missing} "
                f"range_requests={archive.range_requests} full_downloads={archive.full_downloads}"
            )
    archive.close()


@app.command("nbm-parse")
def nbm_parse(
    start: str = typer.Argument(..., help="First UTC date, YYYY-MM-DD"),
    end: str = typer.Argument(..., help="Last UTC date, YYYY-MM-DD"),
    raw: str = typer.Option("data/raw/nbm", help="Root of pulled bulletins"),
    out: str = typer.Option("data/interim/nbm/nbp_periods.parquet", help="Output Parquet file"),
) -> None:
    """Parse pulled NBP blocks into one table of 24-hour forecast periods."""
    from datetime import date as _date
    from pathlib import Path

    from pmw.features.nbm_table import build_table
    from pmw.utils.timeutil import date_range

    _setup_logging()
    days = date_range(_date.fromisoformat(start), _date.fromisoformat(end))
    stats = build_table(Path(raw), days, Path(out))
    typer.echo(f"wrote {out}: {stats}")


@app.command("climo-pull")
def climo_pull(
    start: str = typer.Option("1991-07-01", help="First date"),
    end: str = typer.Option("2026-06-30", help="Last date"),
    out: str = typer.Option("data/interim/climo/daily_snow.parquet", help="Output Parquet file"),
) -> None:
    """Daily snowfall history for every configured station (ACIS), for climatology."""
    from datetime import date as _date
    from pathlib import Path

    import pyarrow as pa
    import pyarrow.parquet as pq

    from pmw.utils.config import load_stations_config
    from pmw.weather.acis import Acis

    _setup_logging()
    acis = Acis(timeout_s=180)
    rows = []
    for icao in sorted(load_stations_config().stations):
        days = acis.snow(icao, _date.fromisoformat(start), _date.fromisoformat(end))
        n_missing = sum(v.missing for v in days.values())
        typer.echo(f"{icao}: {len(days)} days, {n_missing} missing")
        for d, v in days.items():
            rows.append(
                {
                    "station": icao,
                    "date": d,
                    "snow_in": None if v.missing else float(v.measurable),
                    "trace": v.trace,
                    "missing": v.missing,
                }
            )
    schema = pa.schema(
        [
            ("station", pa.string()),
            ("date", pa.date32()),
            ("snow_in", pa.float32()),  # trace counts as 0.0 here; see `trace`
            ("trace", pa.bool_()),
            ("missing", pa.bool_()),
        ]
    )
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), out, compression="zstd")
    typer.echo(f"wrote {out}: {len(rows)} station-days")


@app.command("obs-build")
def obs_build(
    climo: str = typer.Option("data/interim/climo/daily_snow.parquet"),
    out: str = typer.Option("data/interim/obs/daily_snow_settlement.parquet"),
) -> None:
    """Observed daily snowfall at settlement stations (CLI, ACIS for gaps), winters 2021-2026."""
    from pathlib import Path

    from pmw.features.obs_table import build_observed

    _setup_logging()
    stations = ["KNYC", "KBOS", "KDCA", "KPHL", "KORD", "KDTW", "KSEA", "KSLC", "KDFW",
                "KAUS", "KHOU", "KEWR", "KPVD"]  # fmt: skip
    stats = build_observed(stations, list(range(2021, 2027)), Path(climo), Path(out))
    typer.echo(f"wrote {out}: {stats}")


@app.command("nbs-parse")
def nbs_parse(
    start: str = typer.Argument(...),
    end: str = typer.Argument(...),
    raw: str = typer.Option("data/raw/nbm"),
    out: str = typer.Option("data/interim/nbm/nbs_s06.parquet"),
) -> None:
    """Parse pulled NBS blocks into a table of 6-hour snowfall forecasts."""
    from datetime import date as _date
    from pathlib import Path

    from pmw.features.nbm_table import build_nbs_table
    from pmw.utils.timeutil import date_range

    _setup_logging()
    days = date_range(_date.fromisoformat(start), _date.fromisoformat(end))
    typer.echo(f"wrote {out}: {build_nbs_table(Path(raw), days, Path(out))}")


@app.command("p2a-forecasts")
def p2a_forecasts(
    markets: str = typer.Option("data/raw/kalshi/prereg_design/markets.json"),
    out: str = typer.Option("data/processed/p2a/forecasts.parquet"),
    no_nbs: bool = typer.Option(False, help="Ignore NBS 6-hour shares (hours of overlap only)."),
) -> None:
    """P2A stage 1: NBM composite and climatology probabilities at every decision time.
    Reads no prices and no outcomes."""
    from pathlib import Path

    from pmw.eval.p2a import build_forecasts, leakage_violations, load_sample
    from pmw.utils.config import load_stations_config

    _setup_logging()
    events = load_sample(Path(markets), load_stations_config())
    nbs = None if no_nbs else Path("data/interim/nbm/nbs_s06.parquet")
    stats = build_forecasts(
        events,
        nbp_path=Path("data/interim/nbm/nbp_periods.parquet"),
        nbs_path=nbs,
        obs_path=Path("data/interim/obs/daily_snow_settlement.parquet"),
        climo_path=Path("data/interim/climo/daily_snow.parquet"),
        out=Path(out),
    )
    typer.echo(f"wrote {out}: {stats}")
    leaks = leakage_violations(Path(out))
    typer.echo(f"leakage check: {leaks}")
    if leaks["forecast_not_yet_public"] or leaks["observation_not_yet_public"]:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
