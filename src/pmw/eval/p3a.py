"""Experiment P3A (ERCOT daily peak), data stage: Kalshi markets and hourly candles.

Pulls every settled KXTXERCOTPEAKD market and its hourly candles (bid, ask,
volume) over the market's life, verbatim, resumable. Nothing here joins a
price to a forecast or an outcome: that happens only in the registered scoring
stage, after docs/preregistration.md P3A is frozen.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from pmw.markets.kalshi import KalshiError, KalshiPublic

log = logging.getLogger(__name__)

SERIES = "KXTXERCOTPEAKD"
RAW = Path("data/raw/kalshi/p3a")


def _ts(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def pull(client: KalshiPublic, raw: Path = RAW) -> dict[str, Any]:
    raw.mkdir(parents=True, exist_ok=True)
    markets = client.settled_markets(SERIES)
    (raw / "markets.json").write_text(json.dumps(markets))
    fetched = 0
    for i, m in enumerate(markets):
        path = raw / f"candles_{m['ticker']}.json"
        if path.exists():
            continue
        start, end = _ts(m["open_time"]), _ts(m["close_time"])
        try:
            candles = client.candlesticks(SERIES, m["ticker"], start_ts=start, end_ts=end)
        except KalshiError as exc:  # settled before the cutoff: only the historical tier has it
            if exc.status != 404:
                raise
            candles = []
        source = "live"
        if not candles:
            candles = client.historical_candlesticks(m["ticker"], start_ts=start, end_ts=end)
            source = "historical"
        path.write_text(json.dumps({"source": source, "candlesticks": candles}))
        fetched += 1
        if i % 100 == 0:
            log.info("candles %d/%d", i, len(markets))
    return {"markets": len(markets), "fetched": fetched, "requests": client.request_count}
