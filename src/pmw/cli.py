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
    "KXSNOWSTORM", "KXSNOWCITIES",
]  # fmt: skip


@app.command("settle-check")
def settle_check(
    series: str = typer.Argument("KXNYCSNOWM", help="Snow series ticker, or 'all'"),
    brief: bool = typer.Option(False, help="Only print events and mismatches."),
    no_fallback: bool = typer.Option(False, help="Use CLI data only; do not fill gaps from ACIS."),
    include_open: bool = typer.Option(
        False, help="Also compute outcomes for markets Kalshi has not settled yet."
    ),
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
        markets = None
        if include_open:
            markets = client.settled_markets(target)
            seen = {m["ticker"] for m in markets}
            markets += [
                m
                for m in client.markets(series_ticker=target)
                if m["ticker"] not in seen and m.get("result") not in ("yes", "no")
            ]
        checks = check_series(client, iem, target, stations, acis, markets=markets)
        typer.echo(format_checks(checks, target, verbose=not brief and series != "all"))
        for key, value in summarize(checks).items():
            totals[key] = totals.get(key, 0) + value
    client.close()
    if len(targets) > 1:
        typer.echo(f"\nALL SERIES: {totals}")
    if totals["reproduced"] != totals["markets"]:
        raise typer.Exit(1)


def _ercot_client():  # type: ignore[no-untyped-def]
    """ERCOT client from environment credentials. Refuses to run off Railway US."""
    from pmw.energy.ercot import ErcotCredentials, ErcotPublic

    return ErcotPublic(ErcotCredentials.from_env())


@app.command("ercot-probe")
def ercot_probe(
    emils: str = typer.Option("np6-346-cd,np3-565-cd", help="Comma-separated EMIL ids"),
) -> None:
    """[Railway only] Archive extent and document size per ERCOT product; saves one sample each."""
    from pmw.energy.jobs import probe
    from pmw.utils.storage import Storage

    _setup_logging()
    client = _ercot_client()
    out = probe(client, Storage.from_env(), [e.strip() for e in emils.split(",") if e.strip()])
    typer.echo(json.dumps(out, indent=1))
    typer.echo(f"requests: {client.request_count}")
    client.close()


@app.command("ercot-backfill")
def ercot_backfill(
    emil: str = typer.Argument(..., help="EMIL id, e.g. np6-346-cd"),
    start: str = typer.Argument(..., help="First POSTED date (Central), YYYY-MM-DD"),
    end: str = typer.Argument(..., help="Last POSTED date (Central), YYYY-MM-DD"),
    post_hours: str = typer.Option(
        "", help="Keep only docs posted in these Central hours, e.g. 5,11,17"
    ),
) -> None:
    """[Railway only] Save every posted version of an ERCOT report, verbatim. Resumable."""
    from pmw.energy.jobs import backfill
    from pmw.utils.storage import Storage

    _setup_logging()
    hours = {int(h) for h in post_hours.split(",") if h.strip()} or None
    client = _ercot_client()
    stats = backfill(
        client, Storage.from_env(), emil, date.fromisoformat(start), date.fromisoformat(end), hours
    )
    typer.echo(json.dumps(stats))
    typer.echo(f"requests: {client.request_count}")
    client.close()


@app.command("ercot-build")
def ercot_build(
    root: str = typer.Option("data/bucket", help="Local copy of the bucket (make pull-data)"),
    out: str = typer.Option("data/interim/ercot", help="Output directory"),
    forecasts: bool = typer.Option(True, help="Also build forecast tables"),
    telemetry: bool = typer.Option(True, help="Also build real-time load tables (NP3-910-ER)"),
    actuals: bool = typer.Option(True, help="Build actual-load tables"),
) -> None:
    """Build ERCOT actual-load, forecast and telemetry tables from the pulled archive (laptop)."""
    from pathlib import Path

    from pmw.energy.build import build_actuals, build_forecasts, build_telemetry

    _setup_logging()
    if actuals:
        typer.echo(json.dumps({"actuals": build_actuals(Path(root), Path(out))}, indent=1))
    if forecasts:
        typer.echo(json.dumps({"forecasts": build_forecasts(Path(root), Path(out))}, indent=1))
    if telemetry:
        typer.echo(json.dumps({"telemetry": build_telemetry(Path(root), Path(out))}, indent=1))


@app.command("ercot-model")
def ercot_model(
    interim: str = typer.Option("data/interim/ercot"),
    rebuild: bool = typer.Option(False, help="Rebuild the feature dataset"),
) -> None:
    """Phase 4: fit the ERCOT peak models on train, compare on calib and validation (no prices)."""
    from datetime import date as _d
    from pathlib import Path

    import pandas as pd

    from pmw.models import ercot_peak as ep

    _setup_logging()
    root = Path(interim)
    cache = root / "peak_dataset.parquet"
    t = ep.Tables.load(root)
    if rebuild or not cache.exists():
        ds = ep.build_dataset(t, _d(2017, 7, 1), ep.VALID_END)
        ds.to_parquet(cache)
    ds = pd.read_parquet(cache)
    ds["oper_day"] = pd.to_datetime(ds["oper_day"]).dt.date
    leak = int(
        (pd.to_datetime(ds["post_utc"], utc=True) >= pd.to_datetime(ds["t"], utc=True)).sum()
    )
    parts = ep.split(ds)
    gap = ep.telemetry_gap(t, ep.TRAIN_END)
    lgbm = ep.LGBMConformal().fit(parts["train"], parts["calib"])
    models: dict[str, object] = {
        "baseline_p3a": ep.Baseline().fit(parts["train"], parts["calib"]),
        "ngboost": ep.NGBoostNormal().fit(parts["train"], parts["calib"]),
        "lgbm_cqr": lgbm,
    }
    report = {
        "rows": {k: len(v) for k, v in parts.items()},
        "leakage_posting_not_before_t": leak,
        "telemetry_gap_median_pct": round(100 * float(pd.Series(gap).median()), 3),
        "valid": ep.evaluate(models, parts["valid"], gap),
        "lgbm_interval_coverage_valid": ep.interval_coverage(lgbm, parts["valid"]),
    }
    (root / "peak_model_report.json").write_text(json.dumps(report, indent=1, default=str))
    typer.echo(json.dumps(report, indent=1, default=str))


@app.command("ercot-model-v2")
def ercot_model_v2(
    interim: str = typer.Option("data/interim/ercot"),
    rebuild_weather: bool = typer.Option(False, help="Rebuild the weather features"),
) -> None:
    """Phase 4 v2: weather features, more model families, log-odds blend (validation only)."""
    import pickle
    from pathlib import Path

    import pandas as pd

    from pmw.models import ercot_peak as ep
    from pmw.models import ercot_peak_v2 as v2

    _setup_logging()
    root = Path(interim)
    t = ep.Tables.load(root)
    ds = pd.read_parquet(root / "peak_dataset.parquet")
    ds["oper_day"] = pd.to_datetime(ds["oper_day"]).dt.date
    cache = root / "peak_weather.parquet"
    if rebuild_weather or not cache.exists():
        wx = v2.build_weather(ds, t.hourly, Path("data/raw/nbm_ercot"), Path("data/raw/asos"))
        wx.to_parquet(cache)
    wx = pd.read_parquet(cache)
    wx["oper_day"] = pd.to_datetime(wx["oper_day"]).dt.date
    report, fitted = v2.run(ds, wx, t, log=typer.echo)
    (root / "peak_model_v2_report.json").write_text(json.dumps(report, indent=1, default=str))
    with open(root / "peak_model_v2_fitted.pkl", "wb") as f:
        pickle.dump(fitted, f)
    typer.echo(json.dumps(report, indent=1, default=str))


@app.command("ercot-settle-check")
def ercot_settle_check(
    peaks: str = typer.Option("data/interim/ercot/daily_peak.parquet"),
    brief: bool = typer.Option(True, help="Only print mismatches and the summary."),
) -> None:
    """Replicate every settled KXTXERCOTPEAKD market from first-complete NP6-346-CD peaks."""
    import pyarrow.parquet as pq

    from pmw.markets.kalshi import KalshiPublic
    from pmw.settle import ercot as se
    from pmw.utils.config import load_logger_config

    _setup_logging()
    table = pq.read_table(peaks).to_pylist()
    by_day = {
        r["oper_day"]: Decimal(r["peak_first_complete_str"])
        for r in table
        if r["peak_first_complete_str"] is not None
    }
    client = KalshiPublic(load_logger_config().kalshi.base_url, min_request_interval_s=0.5)
    checks = se.check_markets(client.settled_markets("KXTXERCOTPEAKD"), by_day)
    client.close()
    day = None
    for c in checks:
        if not brief and c.oper_day != day:
            day = c.oper_day
            typer.echo(f"{day}: ours {c.our_peak}  kalshi {c.kalshi_value}")
        if not brief or not c.match or c.value_agrees is False:
            typer.echo(
                f"  {c.ticker:<34} > {c.floor}  kalshi={c.kalshi_result} "
                f"ours={c.computed}  value {c.our_peak} vs {c.kalshi_value}"
                f"  {'ok' if c.match else 'MISMATCH'}"
            )
    s = se.summarize(checks)
    typer.echo(json.dumps(s))
    if s["reproduced"] != s["markets"] or s["days_value_differs"]:
        raise typer.Exit(1)


@app.command("p3a")
def p3a(
    raw: str = typer.Option("data/raw/kalshi/p3a"),
    interim: str = typer.Option("data/interim/ercot"),
    out: str = typer.Option("docs/findings/p3a_result_tables.md"),
) -> None:
    """Experiment P3A, registered scoring. Run ONCE (docs/preregistration.md)."""
    from pathlib import Path

    from pmw.eval.p3a_score import load_inputs, report_markdown, score

    _setup_logging()
    markets, quotes, postings, actual = load_inputs(Path(raw), Path(interim))
    r = score(markets, quotes, postings, actual)
    Path("data/processed").mkdir(parents=True, exist_ok=True)
    Path("data/processed/p3a_result.json").write_text(json.dumps(r, indent=1, default=str))
    Path(out).write_text(report_markdown(r))
    typer.echo(report_markdown(r))


@app.command("nbm-pull")
def nbm_pull(
    start: str = typer.Argument(..., help="First UTC date, YYYY-MM-DD"),
    end: str = typer.Argument(..., help="Last UTC date, YYYY-MM-DD"),
    product: str = typer.Option("nbp", help="nbp (probabilistic) or nbs (short range)"),
    hours: str = typer.Option(
        "1,7,13,19",
        help="Cycle hours (UTC), or 'ercot-decisions' for the NBH cycle 2 h before each "
        "ERCOT decision time of that operating day. Only 01/07/13/19Z carry NBP 24-hour "
        "percentiles.",
    ),
    out: str = typer.Option("data/raw/nbm", help="Output root"),
    station_set: str = typer.Option(
        "snow", "--station-set", help="snow (configs/stations.yaml) or ercot (ercot_stations.yaml)"
    ),
) -> None:
    """Download archived NBM text blocks for the configured stations (resumable per day)."""
    from datetime import date as _date
    from pathlib import Path

    from pmw.utils.config import load_ercot_stations_config, load_stations_config
    from pmw.utils.timeutil import date_range
    from pmw.weather.nbm_text import NbmTextArchive, pull_day

    _setup_logging()
    if station_set == "snow":
        stations = sorted(load_stations_config().stations)
    elif station_set == "ercot":
        stations = sorted(load_ercot_stations_config().stations)
        if out == "data/raw/nbm":
            out = "data/raw/nbm_ercot"  # never mix with the snow pull's day files
    else:
        raise typer.BadParameter("--station-set must be snow or ercot")
    if hours == "ercot-decisions":
        from pmw.features.ercot_weather import nbh_cycle_hours_for_day

        def hours_for(day: _date) -> list[int]:
            return nbh_cycle_hours_for_day(day)
    else:
        fixed = [int(h) for h in hours.split(",")]

        def hours_for(day: _date) -> list[int]:
            return fixed

    archive = NbmTextArchive(product)
    root = Path(out)
    total = missing = 0
    days = date_range(_date.fromisoformat(start), _date.fromisoformat(end))
    for done, day in enumerate(days, 1):
        b, m = pull_day(archive, root, day, stations, hours_for(day))
        total += b
        missing += m
        if done % 10 == 0 or done == len(days):
            typer.echo(
                f"{day}  {done}/{len(days)} days  blocks={total} missing={missing} "
                f"range_requests={archive.range_requests} full_downloads={archive.full_downloads}"
            )
    archive.close()


@app.command("asos-pull")
def asos_pull(
    first_year: int = typer.Argument(2020, help="First year"),
    last_year: int = typer.Argument(..., help="Last year (the current year is refreshed)"),
    out: str = typer.Option("data/raw/asos", help="Output root"),
) -> None:
    """Hourly ASOS temperatures (IEM) for the ERCOT stations, one request per station-year."""
    import time as _time
    from pathlib import Path

    import httpx

    from pmw.utils.config import load_ercot_stations_config
    from pmw.weather.asos import pull_year

    _setup_logging()
    stations = [s.asos for s in load_ercot_stations_config().stations.values()]
    with httpx.Client(timeout=300, headers={"User-Agent": "pmw-research/0.1"}) as client:
        for st in stations:
            for year in range(first_year, last_year + 1):
                path = pull_year(client, Path(out), st, year)
                typer.echo(f"{st} {year} {'saved ' + str(path) if path else 'already saved'}")
                if path:
                    _time.sleep(2.0)  # sequential and paced, as IEM asks


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


@app.command("p2a-score")
def p2a_score(
    forecasts: str = typer.Option("data/processed/p2a/forecasts.parquet"),
    markets: str = typer.Option("data/raw/kalshi/prereg_design/markets.json"),
    out_dir: str = typer.Option("data/processed/p2a"),
    figure: str = typer.Option("docs/findings/figures/p2a_reliability.png"),
    report: str = typer.Option("docs/findings/p2a_result_tables.md"),
) -> None:
    """P2A stage 2: join quotes and outcomes, score, simulate, apply the registered rule."""
    from pathlib import Path

    import pandas as pd

    from pmw.eval.p2a import load_sample
    from pmw.eval.p2a_score import (
        load_outcomes,
        load_quotes,
        report_markdown,
        score_all,
        window_ends,
        write_outputs,
    )
    from pmw.utils.config import load_stations_config

    _setup_logging()
    events = load_sample(Path(markets), load_stations_config())
    f = pd.read_parquet(forecasts)
    q = load_quotes(Path(markets).parent, set(f["ticker"]))
    r = score_all(f, q, load_outcomes(Path(markets)), window_ends(Path(markets), events))
    write_outputs(r, Path(out_dir), Path(figure))
    Path(report).write_text(
        report_markdown(
            r,
            Path(figure).name
            if Path(report).parent == Path(figure).parent
            else "figures/" + Path(figure).name,
        )
    )
    typer.echo(f"verdict: {r['verdict']}  primary: {r['primary']}")


if __name__ == "__main__":
    app()
