"""Discovery: find every Kalshi series and open market that matches our filters.

Nothing here hardcodes a ticker. Series are matched by the regexes in
configs/series.yaml, so a new city or a new storm market is picked up on the
next discovery run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pmw.markets.kalshi import KalshiPublic
from pmw.utils.config import KalshiSeriesConfig
from pmw.utils.timeutil import parse_ts, utcnow

log = logging.getLogger(__name__)

# Market statuses in which a book can exist and trade.
TRADABLE_STATUS = {"active", "open"}


@dataclass
class TrackedMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    grp: str
    status: str
    title: str
    yes_sub_title: str
    close_time: datetime | None
    raw: dict[str, Any] = field(repr=False)

    @property
    def ids(self) -> dict[str, str]:
        return {
            "event_ticker": self.event_ticker,
            "series_ticker": self.series_ticker,
            "grp": self.grp,
        }

    @property
    def tradable(self) -> bool:
        return self.status in TRADABLE_STATUS


@dataclass
class DiscoveryResult:
    at: datetime
    series: dict[str, dict[str, Any]]  # matched series ticker -> series object
    series_group: dict[str, str]  # series ticker -> group name
    events: dict[str, dict[str, Any]]  # event ticker -> event object (without markets)
    markets: list[TrackedMarket]
    n_series_scanned: int
    errors: list[str]

    def tradable(self) -> list[TrackedMarket]:
        return [m for m in self.markets if m.tradable]

    def open_count_by_series(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self.tradable():
            out[m.series_ticker] = out.get(m.series_ticker, 0) + 1
        return out


def match_series(
    client: KalshiPublic, cfg: KalshiSeriesConfig, *, all_categories: bool = False
) -> tuple[dict[str, dict[str, Any]], dict[str, str], int]:
    """Return (matched series, their groups, number of series scanned)."""
    scanned: dict[str, dict[str, Any]] = {}
    if all_categories:
        for s in client.series_list():
            scanned[s["ticker"]] = s
    else:
        for category in cfg.categories:
            for s in client.series_list(category):
                scanned[s["ticker"]] = s
    matched: dict[str, dict[str, Any]] = {}
    groups: dict[str, str] = {}
    for ticker, s in scanned.items():
        grp = cfg.classify(ticker, s.get("title") or "", s.get("tags"))
        if grp is not None:
            matched[ticker] = s
            groups[ticker] = grp
    return matched, groups, len(scanned)


def discover(
    client: KalshiPublic, cfg: KalshiSeriesConfig, *, all_categories: bool = False
) -> DiscoveryResult:
    """Scan series, then pull open events and their markets for each match."""
    at = utcnow()
    matched, groups, n_scanned = match_series(client, cfg, all_categories=all_categories)
    events: dict[str, dict[str, Any]] = {}
    markets: list[TrackedMarket] = []
    errors: list[str] = []
    for series_ticker in sorted(matched):
        try:
            series_events = client.events(series_ticker, status="open", with_nested_markets=True)
        except Exception as exc:  # one bad series must not sink the whole run
            errors.append(f"{series_ticker}: {exc}")
            log.warning("discovery failed for %s: %s", series_ticker, exc)
            continue
        for ev in series_events:
            nested = ev.pop("markets", None) or []
            events[ev["event_ticker"]] = ev
            for m in nested:
                markets.append(
                    TrackedMarket(
                        ticker=m["ticker"],
                        event_ticker=m.get("event_ticker") or ev["event_ticker"],
                        series_ticker=series_ticker,
                        grp=groups[series_ticker],
                        status=m.get("status") or "",
                        title=m.get("title") or ev.get("title") or "",
                        yes_sub_title=m.get("yes_sub_title") or "",
                        close_time=parse_ts(m.get("close_time")),
                        raw=m,
                    )
                )
    return DiscoveryResult(
        at=at,
        series=matched,
        series_group=groups,
        events=events,
        markets=markets,
        n_series_scanned=n_scanned,
        errors=errors,
    )


def format_table(result: DiscoveryResult) -> str:
    """Human-readable listing of every open market found, grouped by event."""
    lines: list[str] = []
    by_group: dict[str, dict[str, list[TrackedMarket]]] = {}
    for m in result.tradable():
        by_group.setdefault(m.grp, {}).setdefault(m.event_ticker, []).append(m)
    for grp in sorted(by_group):
        n_mk = sum(len(v) for v in by_group[grp].values())
        lines.append(f"\n== {grp}: {len(by_group[grp])} events, {n_mk} open markets ==")
        for event_ticker in sorted(by_group[grp]):
            ms = by_group[grp][event_ticker]
            ev = result.events.get(event_ticker, {})
            vol = sum(float(m.raw.get("volume_fp") or 0) for m in ms)
            oi = sum(float(m.raw.get("open_interest_fp") or 0) for m in ms)
            close = min((m.close_time for m in ms if m.close_time), default=None)
            lines.append(
                f"{event_ticker:<30} {str(ev.get('title') or '')[:52]:<52} "
                f"mkts={len(ms):<3} vol={vol:>10,.0f} oi={oi:>9,.0f} "
                f"closes={close:%Y-%m-%d %H:%MZ}"
                if close
                else f"{event_ticker:<30} {str(ev.get('title') or '')[:52]:<52} mkts={len(ms)}"
            )
    return "\n".join(lines)
