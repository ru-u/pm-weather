"""Parquet schemas for logger output, and the parsers that fill them.

Every table carries `received_at_utc`, `exchange_ts` (null when the API gives
none), `source` and `schema_version`. Bump SCHEMA_VERSION on any change to a
column's meaning; add columns rather than repurposing them.

Prices are dollars as float64. Kalshi sends 4-decimal strings ("0.5600"), which
float64 holds to well under a tenth of a tick; round to 4 dp when comparing.
Sizes are contracts as float64 (Kalshi allows fractional contracts, 2 dp).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

import pyarrow as pa

from pmw.utils.timeutil import parse_ts

SCHEMA_VERSION = 1
SOURCE_KALSHI = "kalshi_rest"

_TS = pa.timestamp("us", tz="UTC")
_COMMON = [
    ("received_at_utc", _TS),
    ("exchange_ts", _TS),
    ("source", pa.string()),
    ("schema_version", pa.int8()),
]
_IDS = [
    ("ticker", pa.string()),
    ("event_ticker", pa.string()),
    ("series_ticker", pa.string()),
    ("grp", pa.string()),
]

ORDERBOOK = pa.schema(
    [
        *_COMMON,
        *_IDS,
        ("trigger", pa.string()),  # base | hot | burst
        # Resting bids exactly as received: ascending price, best bid LAST.
        ("yes_px", pa.list_(pa.float64())),
        ("yes_qty", pa.list_(pa.float64())),
        ("no_px", pa.list_(pa.float64())),
        ("no_qty", pa.list_(pa.float64())),
        # Derived top of book in YES terms. yes_ask = 1 - best NO bid.
        ("best_yes_bid", pa.float64()),
        ("best_yes_bid_qty", pa.float64()),
        ("best_yes_ask", pa.float64()),
        ("best_yes_ask_qty", pa.float64()),
    ]
)

MARKET = pa.schema(
    [
        *_COMMON,  # exchange_ts = market.updated_time
        *_IDS,
        ("status", pa.string()),
        ("yes_bid", pa.float64()),
        ("yes_ask", pa.float64()),
        ("yes_bid_size", pa.float64()),
        ("yes_ask_size", pa.float64()),
        ("last_price", pa.float64()),
        ("volume", pa.float64()),
        ("volume_24h", pa.float64()),
        ("open_interest", pa.float64()),
        ("result", pa.string()),
        ("expiration_value", pa.string()),
    ]
)

TRADE = pa.schema(
    [
        *_COMMON,  # exchange_ts = trade.created_time
        ("trade_id", pa.string()),
        ("ticker", pa.string()),
        ("series_ticker", pa.string()),
        ("grp", pa.string()),
        ("yes_price", pa.float64()),
        ("no_price", pa.float64()),
        ("count", pa.float64()),
        ("taker_side", pa.string()),
        ("taker_outcome_side", pa.string()),
        ("taker_book_side", pa.string()),
        ("is_block_trade", pa.bool_()),
    ]
)

MARKET_META = pa.schema(
    [
        *_COMMON,  # exchange_ts = market.updated_time
        *_IDS,
        ("title", pa.string()),
        ("event_title", pa.string()),
        ("event_sub_title", pa.string()),
        ("yes_sub_title", pa.string()),
        ("market_type", pa.string()),
        ("strike_type", pa.string()),
        ("floor_strike", pa.float64()),
        ("cap_strike", pa.float64()),
        ("status", pa.string()),
        ("open_time", _TS),
        ("close_time", _TS),
        ("expected_expiration_time", _TS),
        ("latest_expiration_time", _TS),
        ("rules_primary", pa.string()),
        ("rules_secondary", pa.string()),
        ("rules_sha256", pa.string()),  # hash of the two rules fields: versions rule changes
        ("meta_sha256", pa.string()),  # hash of all slow-changing fields
        ("settlement_sources", pa.string()),  # JSON
        ("contract_terms_url", pa.string()),
        ("fee_type", pa.string()),
        ("fee_multiplier", pa.float64()),
        ("raw_json", pa.string()),  # the full market object, for lossless re-parsing
    ]
)

SERIES_META = pa.schema(
    [
        *_COMMON,
        ("series_ticker", pa.string()),
        ("grp", pa.string()),
        ("title", pa.string()),
        ("category", pa.string()),
        ("tags", pa.string()),  # JSON
        ("frequency", pa.string()),
        ("fee_type", pa.string()),
        ("fee_multiplier", pa.float64()),
        ("settlement_sources", pa.string()),  # JSON
        ("contract_terms_url", pa.string()),
        ("n_open_markets", pa.int32()),
        ("meta_sha256", pa.string()),
    ]
)

HEALTH = pa.schema(
    [
        *_COMMON,
        ("kind", pa.string()),  # orderbooks_base | orderbooks_hot | markets | trades | discovery
        ("n_requested", pa.int32()),
        ("n_ok", pa.int32()),
        ("n_err", pa.int32()),
        ("duration_s", pa.float64()),
        ("rss_mb", pa.float64()),
        ("note", pa.string()),
    ]
)

TABLES: dict[str, tuple[str, pa.Schema]] = {
    # table name -> (source directory, schema)
    "orderbooks": ("kalshi", ORDERBOOK),
    "markets": ("kalshi", MARKET),
    "trades": ("kalshi", TRADE),
    "market_meta": ("kalshi", MARKET_META),
    "series_meta": ("kalshi", SERIES_META),
    "health": ("pmw", HEALTH),
}


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _levels(raw: Any) -> tuple[list[float], list[float]]:
    px: list[float] = []
    qty: list[float] = []
    for level in raw or []:
        px.append(float(level[0]))
        qty.append(float(level[1]))
    return px, qty


def sha256_json(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def orderbook_row(
    ticker: str,
    book: dict[str, Any],
    received: datetime,
    ids: dict[str, str],
    trigger: str,
) -> dict[str, Any]:
    """One row per (market, snapshot). `book` is Kalshi's `orderbook_fp` object."""
    yes_px, yes_qty = _levels(book.get("yes_dollars"))
    no_px, no_qty = _levels(book.get("no_dollars"))
    return {
        "received_at_utc": received,
        "exchange_ts": None,  # the REST book carries no exchange timestamp
        "source": SOURCE_KALSHI,
        "schema_version": SCHEMA_VERSION,
        "ticker": ticker,
        "event_ticker": ids.get("event_ticker"),
        "series_ticker": ids.get("series_ticker"),
        "grp": ids.get("grp"),
        "trigger": trigger,
        "yes_px": yes_px,
        "yes_qty": yes_qty,
        "no_px": no_px,
        "no_qty": no_qty,
        "best_yes_bid": yes_px[-1] if yes_px else None,
        "best_yes_bid_qty": yes_qty[-1] if yes_px else None,
        "best_yes_ask": round(1.0 - no_px[-1], 4) if no_px else None,
        "best_yes_ask_qty": no_qty[-1] if no_px else None,
    }


def market_row(m: dict[str, Any], received: datetime, ids: dict[str, str]) -> dict[str, Any]:
    return {
        "received_at_utc": received,
        "exchange_ts": parse_ts(m.get("updated_time")),
        "source": SOURCE_KALSHI,
        "schema_version": SCHEMA_VERSION,
        "ticker": m["ticker"],
        "event_ticker": m.get("event_ticker") or ids.get("event_ticker"),
        "series_ticker": ids.get("series_ticker"),
        "grp": ids.get("grp"),
        "status": m.get("status"),
        "yes_bid": _f(m.get("yes_bid_dollars")),
        "yes_ask": _f(m.get("yes_ask_dollars")),
        "yes_bid_size": _f(m.get("yes_bid_size_fp")),
        "yes_ask_size": _f(m.get("yes_ask_size_fp")),
        "last_price": _f(m.get("last_price_dollars")),
        "volume": _f(m.get("volume_fp")),
        "volume_24h": _f(m.get("volume_24h_fp")),
        "open_interest": _f(m.get("open_interest_fp")),
        "result": m.get("result") or None,
        "expiration_value": m.get("expiration_value") or None,
    }


def trade_row(t: dict[str, Any], received: datetime, ids: dict[str, str]) -> dict[str, Any]:
    return {
        "received_at_utc": received,
        "exchange_ts": parse_ts(t.get("created_time")),
        "source": SOURCE_KALSHI,
        "schema_version": SCHEMA_VERSION,
        "trade_id": t["trade_id"],
        "ticker": t["ticker"],
        "series_ticker": ids.get("series_ticker"),
        "grp": ids.get("grp"),
        "yes_price": _f(t.get("yes_price_dollars")),
        "no_price": _f(t.get("no_price_dollars")),
        "count": _f(t.get("count_fp")),
        "taker_side": t.get("taker_side"),
        "taker_outcome_side": t.get("taker_outcome_side"),
        "taker_book_side": t.get("taker_book_side"),
        "is_block_trade": t.get("is_block_trade"),
    }


# Fields that change with every tick. Excluded from the metadata hash so a new
# market_meta row is written only when something structural changes.
_FAST_FIELDS = {
    "last_price_dollars",
    "no_ask_dollars",
    "no_bid_dollars",
    "yes_ask_dollars",
    "yes_bid_dollars",
    "yes_ask_size_fp",
    "yes_bid_size_fp",
    "open_interest_fp",
    "previous_price_dollars",
    "previous_yes_ask_dollars",
    "previous_yes_bid_dollars",
    "volume_24h_fp",
    "volume_fp",
    "updated_time",
    "liquidity_dollars",
}


def market_meta_row(
    m: dict[str, Any],
    event: dict[str, Any],
    series: dict[str, Any],
    received: datetime,
    grp: str,
) -> dict[str, Any]:
    slow = {k: v for k, v in m.items() if k not in _FAST_FIELDS}
    return {
        "received_at_utc": received,
        "exchange_ts": parse_ts(m.get("updated_time")),
        "source": SOURCE_KALSHI,
        "schema_version": SCHEMA_VERSION,
        "ticker": m["ticker"],
        "event_ticker": m.get("event_ticker") or event.get("event_ticker"),
        "series_ticker": series.get("ticker"),
        "grp": grp,
        "title": m.get("title"),
        "event_title": event.get("title"),
        "event_sub_title": event.get("sub_title"),
        "yes_sub_title": m.get("yes_sub_title"),
        "market_type": m.get("market_type"),
        "strike_type": m.get("strike_type"),
        "floor_strike": _f(m.get("floor_strike")),
        "cap_strike": _f(m.get("cap_strike")),
        "status": m.get("status"),
        "open_time": parse_ts(m.get("open_time")),
        "close_time": parse_ts(m.get("close_time")),
        "expected_expiration_time": parse_ts(m.get("expected_expiration_time")),
        "latest_expiration_time": parse_ts(m.get("latest_expiration_time")),
        "rules_primary": m.get("rules_primary"),
        "rules_secondary": m.get("rules_secondary"),
        "rules_sha256": sha256_json([m.get("rules_primary"), m.get("rules_secondary")]),
        "meta_sha256": sha256_json(
            [slow, event.get("title"), series.get("settlement_sources"), series.get("fee_type")]
        ),
        "settlement_sources": json.dumps(series.get("settlement_sources")),
        "contract_terms_url": series.get("contract_terms_url"),
        "fee_type": series.get("fee_type"),
        "fee_multiplier": _f(series.get("fee_multiplier")),
        "raw_json": json.dumps(m, sort_keys=True),
    }


def series_meta_row(
    s: dict[str, Any], received: datetime, grp: str, n_open_markets: int
) -> dict[str, Any]:
    return {
        "received_at_utc": received,
        "exchange_ts": parse_ts(s.get("last_updated_ts")),
        "source": SOURCE_KALSHI,
        "schema_version": SCHEMA_VERSION,
        "series_ticker": s["ticker"],
        "grp": grp,
        "title": s.get("title"),
        "category": s.get("category"),
        "tags": json.dumps(s.get("tags")),
        "frequency": s.get("frequency"),
        "fee_type": s.get("fee_type"),
        "fee_multiplier": _f(s.get("fee_multiplier")),
        "settlement_sources": json.dumps(s.get("settlement_sources")),
        "contract_terms_url": s.get("contract_terms_url"),
        "n_open_markets": n_open_markets,
        "meta_sha256": sha256_json({k: v for k, v in s.items() if k != "last_updated_ts"}),
    }
