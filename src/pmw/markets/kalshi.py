"""Kalshi PUBLIC market-data client (REST, no authentication).

This module deliberately contains no signing code and reads no credentials.
Kalshi's market-data REST endpoints are public (verified 2026-11-14), so the
logger never holds a key that could place an order. Authenticated trading, when
it exists, lives in a separate module used only by the trading service.

Docs: https://docs.kalshi.com  (index: https://docs.kalshi.com/llms.txt)
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from pmw.utils.timeutil import utcnow

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
MAX_ORDERBOOK_BATCH = 100  # server rejects more than 100 tickers per call (tested)
MAX_MARKETS_BATCH = 100
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

RawSink = Callable[[str, dict[str, Any], datetime, Any], None]


class KalshiError(RuntimeError):
    def __init__(self, status: int, path: str, body: str) -> None:
        super().__init__(f"Kalshi {status} on {path}: {body[:300]}")
        self.status = status
        self.path = path


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, KalshiError):
        return exc.status in RETRYABLE_STATUS
    # Dropped connections, timeouts, and truncated bodies all retry.
    return isinstance(exc, httpx.TransportError | ValueError)


def _log_retry(state: RetryCallState) -> None:
    exc = state.outcome.exception() if state.outcome else None
    log.warning("kalshi retry #%d after %s", state.attempt_number, exc)


def _chunks(items: list[str], n: int) -> Iterator[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


class KalshiPublic:
    """Thin, paced, retrying client for Kalshi's unauthenticated endpoints."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        min_request_interval_s: float = 0.25,
        timeout_s: float = 20.0,
        max_attempts: int = 6,
        user_agent: str = "pmw-research-logger/0.1",
        raw_sink: RawSink | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._min_interval = min_request_interval_s
        self._last_request = 0.0
        self._raw_sink = raw_sink
        self.request_count = 0
        self._client = httpx.Client(
            timeout=timeout_s,
            headers={"Accept": "application/json", "User-Agent": user_agent},
            transport=transport,
        )
        self._get = retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=0.5, max=30),
            before_sleep=_log_retry,
            reraise=True,
        )(self._get_once)

    def close(self) -> None:
        self._client.close()

    # ---- transport ----

    def _pace(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.02))
        self._last_request = time.monotonic()

    def _get_once(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._pace()
        self.request_count += 1
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        resp = self._client.get(self.base_url + path, params=clean)
        received = utcnow()
        if resp.status_code != 200:
            raise KalshiError(resp.status_code, path, resp.text)
        body = resp.json()  # ValueError on a truncated body, which retries
        if not isinstance(body, dict):
            raise ValueError(f"unexpected JSON shape on {path}")
        if self._raw_sink is not None:
            self._raw_sink(path, clean, received, body)
        return body

    def _paginate(
        self, path: str, key: str, params: dict[str, Any], max_pages: int = 500
    ) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        for _ in range(max_pages):
            body = self._get(path, {**params, "cursor": cursor})
            yield from body.get(key) or []
            cursor = body.get("cursor") or None
            if not cursor:
                return
        log.warning("pagination cap hit on %s", path)

    # ---- metadata ----

    def exchange_status(self) -> dict[str, Any]:
        return self._get("/exchange/status")

    def series_list(self, category: str | None = None) -> list[dict[str, Any]]:
        """All series, or those in one category (exact, case-sensitive match)."""
        return list(self._get("/series", {"category": category}).get("series") or [])

    def series(self, series_ticker: str) -> dict[str, Any]:
        return dict(self._get(f"/series/{series_ticker}")["series"])

    def events(
        self,
        series_ticker: str,
        *,
        status: str | None = None,
        with_nested_markets: bool = True,
    ) -> list[dict[str, Any]]:
        params = {
            "series_ticker": series_ticker,
            "status": status,
            "with_nested_markets": "true" if with_nested_markets else None,
            "limit": 200,
        }
        return list(self._paginate("/events", "events", params))

    def markets(
        self,
        *,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        tickers: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if tickers is not None:
            out: list[dict[str, Any]] = []
            for chunk in _chunks(tickers, MAX_MARKETS_BATCH):
                out.extend(
                    self._paginate(
                        "/markets", "markets", {"tickers": ",".join(chunk), "limit": 1000}
                    )
                )
            return out
        params = {
            "series_ticker": series_ticker,
            "event_ticker": event_ticker,
            "status": status,
            "limit": 1000,
        }
        return list(self._paginate("/markets", "markets", params))

    # ---- books and prints ----

    def orderbook(self, ticker: str, depth: int | None = None) -> dict[str, Any]:
        """Full-depth book for one market. Returns the `orderbook_fp` object."""
        return dict(self._get(f"/markets/{ticker}/orderbook", {"depth": depth})["orderbook_fp"])

    def orderbooks(self, tickers: list[str]) -> list[tuple[str, dict[str, Any], datetime]]:
        """Full-depth books for many markets, batched 100 per request.

        Returns (ticker, orderbook_fp, received_at) so each book carries the
        receive time of the batch it actually arrived in.
        """
        out: list[tuple[str, dict[str, Any], datetime]] = []
        for chunk in _chunks(tickers, MAX_ORDERBOOK_BATCH):
            body = self._get("/markets/orderbooks", {"tickers": chunk})
            received = utcnow()
            for item in body.get("orderbooks") or []:
                out.append((item["ticker"], item.get("orderbook_fp") or {}, received))
        return out

    def trades(
        self,
        ticker: str | None = None,
        *,
        min_ts: int | None = None,
        max_ts: int | None = None,
        max_pages: int = 200,
    ) -> list[dict[str, Any]]:
        params = {"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts, "limit": 1000}
        return list(self._paginate("/markets/trades", "trades", params, max_pages=max_pages))

    def candlesticks(
        self,
        series_ticker: str,
        ticker: str,
        *,
        start_ts: int,
        end_ts: int,
        period_interval: int = 60,
    ) -> list[dict[str, Any]]:
        """period_interval is in minutes: 1, 60 or 1440."""
        body = self._get(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )
        return list(body.get("candlesticks") or [])

    # ---- historical tier (settled before the cutoff) ----

    def historical_cutoff(self) -> dict[str, Any]:
        return self._get("/historical/cutoff")

    def historical_markets(
        self, *, series_ticker: str | None = None, event_ticker: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"series_ticker": series_ticker, "event_ticker": event_ticker, "limit": 1000}
        return list(self._paginate("/historical/markets", "markets", params))

    def historical_trades(
        self, ticker: str, *, min_ts: int | None = None, max_ts: int | None = None
    ) -> list[dict[str, Any]]:
        params = {"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts, "limit": 1000}
        return list(self._paginate("/historical/trades", "trades", params))

    def historical_candlesticks(
        self, ticker: str, *, start_ts: int, end_ts: int, period_interval: int = 60
    ) -> list[dict[str, Any]]:
        body = self._get(
            f"/historical/markets/{ticker}/candlesticks",
            {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )
        return list(body.get("candlesticks") or [])

    def settled_markets(self, series_ticker: str) -> list[dict[str, Any]]:
        """Every settled market in a series, across the live and historical tiers."""
        seen: dict[str, dict[str, Any]] = {}
        for m in self.historical_markets(series_ticker=series_ticker):
            seen[m["ticker"]] = m
        for m in self.markets(series_ticker=series_ticker, status="settled"):
            seen[m["ticker"]] = m
        return sorted(seen.values(), key=lambda m: m["ticker"])
