"""The always-on market logger loop.

Polls Kalshi's public REST API only. It holds no trading credentials and has no
code path that could send an order.

Each base cycle (default every 5 min, aligned to the wall clock):
  1. full-depth orderbooks for every tradable tracked market (100 per request)
  2. market snapshots (bid/ask/volume/open interest/status)
  3. trades for any market whose volume moved since the last cycle
Between base cycles, "hot" markets get extra orderbook snapshots (default 1 min).
"""

from __future__ import annotations

import logging
import os
import resource
import signal
import sys
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any

import httpx
import pyarrow as pa

from pmw.logger import schemas
from pmw.logger.buffer import RawSink, TableBuffers
from pmw.logger.discovery import DiscoveryResult, TrackedMarket, discover
from pmw.markets.kalshi import KalshiPublic
from pmw.utils.config import LoggerConfig, SeriesConfig
from pmw.utils.storage import Storage
from pmw.utils.timeutil import utcnow

log = logging.getLogger(__name__)

SETTLED_STATUS = {"finalized", "settled", "determined"}
MAX_SEEN_TRADE_IDS = 200_000
KEEP_CLOSED_DAYS = 10
MARKETS_BATCH = 100
# Discovery takes about a minute. Start it 150 s after the interval boundary so
# it never delays the base snapshot that lands on the hour.
DISCOVERY_OFFSET_S = 150


def rss_mb() -> float:
    """Current resident memory in MB (Linux), or peak RSS where /proc is absent."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1e6
    except OSError:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / 1e6 if sys.platform == "darwin" else peak / 1e3


def send_alert(message: str) -> None:
    """Post to PMW_ALERT_WEBHOOK (Discord- or Slack-style) if one is configured."""
    url = os.environ.get("PMW_ALERT_WEBHOOK")
    log.error("ALERT: %s", message)
    if not url:
        return
    try:
        httpx.post(url, json={"content": message, "text": message}, timeout=10)
    except Exception:
        log.exception("alert webhook failed")


def _next_boundary(now: float, interval: int) -> float:
    """Next wall-clock multiple of `interval` seconds strictly after `now`."""
    return (int(now) // interval + 1) * interval


def _in_window(now: datetime, windows: list[tuple[str, str]]) -> bool:
    hm = now.strftime("%H:%M")
    for start, end in windows:
        if (start <= hm < end) if start <= end else (hm >= start or hm < end):
            return True
    return False


class MarketLogger:
    def __init__(
        self,
        cfg: LoggerConfig,
        series_cfg: SeriesConfig,
        storage: Storage,
        client: KalshiPublic | None = None,
    ) -> None:
        self.cfg = cfg
        self.series_cfg = series_cfg
        self.storage = storage
        self.buffers = TableBuffers(storage)
        self.raw = RawSink(storage, enabled=cfg.raw.enabled)
        self.client = client or KalshiPublic(
            cfg.kalshi.base_url,
            min_request_interval_s=cfg.kalshi.min_request_interval_s,
            timeout_s=cfg.kalshi.timeout_s,
            max_attempts=cfg.kalshi.max_attempts,
            user_agent=cfg.kalshi.user_agent,
            raw_sink=self.raw,
        )
        self.tracked: dict[str, TrackedMarket] = {}
        self.last_discovery: DiscoveryResult | None = None
        self._last_volume: dict[str, float] = {}
        self._last_trade_ts: dict[str, int] = {}
        # Markets whose volume moved and whose trades are still to be fetched,
        # mapped to the earliest timestamp needed. Drained one request at a time
        # in the gaps between snapshots, so trade polling never delays a book.
        self._trade_queue: OrderedDict[str, int] = OrderedDict()
        self._trade_stats = {"markets": 0, "new": 0, "err": 0, "seconds": 0.0}
        self._hot_until: dict[str, datetime] = {}  # keyed by event ticker
        self._seen_trades: OrderedDict[str, None] = OrderedDict()
        self._seen_meta: dict[str, str] = {}
        self._seen_series: dict[str, str] = {}
        self._closed_since: dict[str, datetime] = {}
        self._consecutive_failures = 0
        self._stop = False
        self.base_cycles = 0
        self.hot_cycles = 0

    # ---- plumbing ----

    def _health(
        self, kind: str, started: float, n_req: int, n_ok: int, n_err: int, note: str = ""
    ) -> None:
        self.buffers.add(
            "health",
            {
                "received_at_utc": utcnow(),
                "exchange_ts": None,
                "source": "pmw_logger",
                "schema_version": schemas.SCHEMA_VERSION,
                "kind": kind,
                "n_requested": n_req,
                "n_ok": n_ok,
                "n_err": n_err,
                "duration_s": round(time.monotonic() - started, 3),
                "rss_mb": round(rss_mb(), 1),
                "note": note[:500],
            },
        )

    def flush(self) -> None:
        written = self.buffers.flush()
        raw_bytes = self.raw.flush()
        # Arrow keeps freed buffers in its pool. Hand them back to the OS so the
        # footprint between flushes stays near the idle level (memory is billed).
        pa.default_memory_pool().release_unused()
        if written or raw_bytes:
            log.info(
                "flush: %s raw=%dB rss=%.0fMB",
                " ".join(f"{k}={v}B" for k, v in written.items()),
                raw_bytes,
                rss_mb(),
            )

    # ---- discovery ----

    def run_discovery(self) -> None:
        started = time.monotonic()
        try:
            result = discover(self.client, self.series_cfg.kalshi)
        except Exception as exc:
            log.exception("discovery failed")
            self._health("discovery", started, 0, 0, 1, str(exc))
            return
        self.last_discovery = result
        now = result.at
        open_counts = result.open_count_by_series()

        for ticker, s in result.series.items():
            row = schemas.series_meta_row(
                s, now, result.series_group[ticker], open_counts.get(ticker, 0)
            )
            if self._seen_series.get(ticker) != row["meta_sha256"]:
                self._seen_series[ticker] = row["meta_sha256"]
                self.buffers.add("series_meta", row)

        fresh: dict[str, TrackedMarket] = {}
        for m in result.markets:
            fresh[m.ticker] = m
            row = schemas.market_meta_row(
                m.raw,
                result.events.get(m.event_ticker, {}),
                result.series[m.series_ticker],
                now,
                m.grp,
            )
            if self._seen_meta.get(m.ticker) != row["meta_sha256"]:
                if m.ticker in self._seen_meta:
                    log.info("metadata changed for %s", m.ticker)
                self._seen_meta[m.ticker] = row["meta_sha256"]
                self.buffers.add("market_meta", row)

        # Keep following markets that dropped out of the open list until they
        # settle, so the final result and expiration value get recorded.
        for ticker, old in self.tracked.items():
            if ticker in fresh:
                continue
            since = self._closed_since.setdefault(ticker, now)
            if old.status not in SETTLED_STATUS and now - since < timedelta(days=KEEP_CLOSED_DAYS):
                fresh[ticker] = old
        new = sorted(set(fresh) - set(self.tracked))
        if new and self.tracked:
            log.info("discovery: %d new markets, e.g. %s", len(new), new[:5])
        self.tracked = fresh
        n_tradable = sum(1 for m in fresh.values() if m.tradable)
        log.info(
            "discovery: scanned %d series, matched %d, tracking %d markets (%d tradable), "
            "%d errors",
            result.n_series_scanned,
            len(result.series),
            len(fresh),
            n_tradable,
            len(result.errors),
        )
        self._health(
            "discovery",
            started,
            len(result.series),
            len(result.series) - len(result.errors),
            len(result.errors),
            f"tracked={len(fresh)} tradable={n_tradable}",
        )

    # ---- snapshots ----

    def hot_tickers(self, now: datetime) -> tuple[list[str], str]:
        """Markets that get the fast cadence right now, and why."""
        groups = self.series_cfg.kalshi.groups
        burst = _in_window(now, self.cfg.bursts.windows_utc)
        out: list[str] = []
        for m in self.tracked.values():
            if not m.tradable:
                continue
            g = groups.get(m.grp)
            hot = False
            if g is not None:
                if g.always_hot:
                    hot = True
                elif g.hot_close_within_hours > 0 and m.close_time is not None:
                    hot = m.close_time - now <= timedelta(hours=g.hot_close_within_hours)
            until = self._hot_until.get(m.event_ticker)
            if until is not None and until > now:
                hot = True
            if burst and m.grp in self.cfg.bursts.groups:
                hot = True
            if hot:
                out.append(m.ticker)
        return out, ("burst" if burst else "hot")

    def snapshot_orderbooks(self, tickers: list[str], trigger: str) -> bool:
        if not tickers:
            return True
        started = time.monotonic()
        try:
            books = self.client.orderbooks(tickers)
        except Exception as exc:
            log.warning("orderbook snapshot failed: %s", exc)
            self._health(f"orderbooks_{trigger}", started, len(tickers), 0, len(tickers), str(exc))
            return False
        got = 0
        for ticker, book, received in books:
            m = self.tracked.get(ticker)
            if m is None:
                continue
            self.buffers.add(
                "orderbooks", schemas.orderbook_row(ticker, book, received, m.ids, trigger)
            )
            got += 1
        self._health(f"orderbooks_{trigger}", started, len(tickers), got, len(tickers) - got)
        return True

    def snapshot_markets(self) -> bool:
        tickers = list(self.tracked)
        if not tickers:
            return True
        started = time.monotonic()
        # One request per 100 tickers. A batch that is rate-limited out must not
        # throw away the batches that worked.
        markets: list[dict[str, Any]] = []
        failed = 0
        last_error = ""
        for i in range(0, len(tickers), MARKETS_BATCH):
            chunk = tickers[i : i + MARKETS_BATCH]
            try:
                markets.extend(self.client.markets(tickers=chunk))
            except Exception as exc:
                failed += len(chunk)
                last_error = str(exc)
                log.warning("market snapshot batch failed: %s", exc)
        if not markets:
            self._health("markets", started, len(tickers), 0, len(tickers), last_error)
            return False
        now = utcnow()
        lookback = int(self.cfg.trades.startup_lookback_h * 3600)
        for raw in markets:
            m = self.tracked.get(raw["ticker"])
            if m is None:
                continue
            self.buffers.add("markets", schemas.market_row(raw, now, m.ids))
            m.status = raw.get("status") or m.status
            volume = float(raw.get("volume_fp") or 0)
            prev = self._last_volume.get(m.ticker)
            if prev is None:
                # First sight after a (re)start: re-pull recent history, so a
                # restart leaves no hole in the trade tape.
                if float(raw.get("volume_24h_fp") or 0) > 0:
                    self._queue_trades(m.ticker, int(now.timestamp()) - lookback)
            elif volume > prev:
                since = self._last_trade_ts.get(m.ticker, int(now.timestamp()) - lookback)
                self._queue_trades(m.ticker, since - 5)  # small overlap; ids dedupe
                if volume - prev >= self.cfg.hot.volume_spike_contracts:
                    self._hot_until[m.event_ticker] = now + timedelta(
                        seconds=self.cfg.hot.volume_hot_hold_s
                    )
            self._last_volume[m.ticker] = volume
        self._health("markets", started, len(tickers), len(markets), failed, last_error)
        return True

    def _queue_trades(self, ticker: str, min_ts: int) -> None:
        self._trade_queue[ticker] = min(min_ts, self._trade_queue.get(ticker, min_ts))

    def drain_one_trade_fetch(self) -> bool:
        """Fetch trades for one queued market. Returns False when the queue is empty."""
        if not self._trade_queue:
            return False
        ticker, min_ts = self._trade_queue.popitem(last=False)
        m = self.tracked.get(ticker)
        if m is None:
            return True
        started = time.monotonic()
        self._trade_stats["markets"] += 1
        try:
            trades = self.client.trades(ticker, min_ts=min_ts)
        except Exception as exc:
            self._trade_stats["err"] += 1
            self._trade_queue[ticker] = min_ts  # back of the queue; try again later
            log.warning("trades failed for %s: %s", ticker, exc)
            return True
        received = utcnow()
        newest = self._last_trade_ts.get(ticker, 0)
        for t in trades:
            row = schemas.trade_row(t, received, m.ids)
            if row["exchange_ts"] is not None:
                newest = max(newest, int(row["exchange_ts"].timestamp()))
            if t["trade_id"] in self._seen_trades:
                continue
            self._seen_trades[t["trade_id"]] = None
            self.buffers.add("trades", row)
            self._trade_stats["new"] += 1
        if newest:
            self._last_trade_ts[ticker] = newest
        while len(self._seen_trades) > MAX_SEEN_TRADE_IDS:
            self._seen_trades.popitem(last=False)
        self._trade_stats["seconds"] += time.monotonic() - started
        return True

    def _trade_health(self) -> None:
        st = self._trade_stats
        if st["markets"]:
            self._health(
                "trades",
                time.monotonic() - st["seconds"],
                int(st["markets"]),
                int(st["markets"] - st["err"]),
                int(st["err"]),
                f"new={int(st['new'])} queued={len(self._trade_queue)}",
            )
        self._trade_stats = {"markets": 0, "new": 0, "err": 0, "seconds": 0.0}

    def base_cycle(self) -> None:
        self._trade_health()
        tradable = [t for t, m in self.tracked.items() if m.tradable]
        ok_books = self.snapshot_orderbooks(tradable, "base")
        ok_markets = self.snapshot_markets()
        self.base_cycles += 1
        log.info(
            "base cycle %d: books=%d markets=%d trade_queue=%d ok=%s rss=%.0fMB",
            self.base_cycles,
            len(tradable),
            len(self.tracked),
            len(self._trade_queue),
            ok_books and ok_markets,
            rss_mb(),
        )
        if ok_books and ok_markets:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
            if self._consecutive_failures == 3:
                send_alert("pmw logger: 3 consecutive base cycles failed")

    # ---- main loop ----

    def stop(self, *_: object) -> None:
        self._stop = True

    def run(
        self,
        max_base_cycles: int | None = None,
        align: bool = True,
        drain_on_exit_s: float = 0.0,
    ) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        cad = self.cfg.cadence
        log.info(
            "logger starting: storage=%s base=%ds hot=%ds",
            self.storage.describe(),
            cad.orderbook_base_s,
            cad.orderbook_hot_s,
        )
        self.run_discovery()
        now = time.time()
        due_base = _next_boundary(now, cad.orderbook_base_s) if align else now
        due_hot = _next_boundary(now, cad.orderbook_hot_s)
        due_discovery = _next_boundary(now, cad.discovery_s) + DISCOVERY_OFFSET_S
        due_flush = now + cad.flush_s
        try:
            while not self._stop:
                now = time.time()
                if now >= due_discovery:
                    self.run_discovery()
                    due_discovery = (
                        _next_boundary(time.time(), cad.discovery_s) + DISCOVERY_OFFSET_S
                    )
                if now >= due_base:
                    self.base_cycle()
                    due_base = _next_boundary(time.time(), cad.orderbook_base_s)
                    due_hot = _next_boundary(time.time(), cad.orderbook_hot_s)
                    if max_base_cycles is not None and self.base_cycles >= max_base_cycles:
                        break
                elif now >= due_hot:
                    tickers, trigger = self.hot_tickers(utcnow())
                    if tickers:
                        self.snapshot_orderbooks(tickers, trigger)
                        self.hot_cycles += 1
                    due_hot = _next_boundary(time.time(), cad.orderbook_hot_s)
                if time.time() >= due_flush or self.buffers.pending_rows() >= cad.flush_max_rows:
                    self.flush()
                    due_flush = time.time() + cad.flush_s
                wake = min(due_base, due_hot, due_discovery, due_flush)
                # Spend idle time on queued trade fetches, one request at a time,
                # but never start one that could run into the next snapshot.
                if self._trade_queue and wake - time.time() > 3.0:
                    self.drain_one_trade_fetch()
                else:
                    time.sleep(max(0.05, min(1.0, wake - time.time())))
            deadline = time.time() + drain_on_exit_s
            while not self._stop and time.time() < deadline and self.drain_one_trade_fetch():
                pass
        finally:
            self._trade_health()
            log.info("logger stopping: flushing buffers")
            self.flush()
            self.client.close()
