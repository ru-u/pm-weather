"""Fee-aware taker backtest on logged Kalshi order books.

Kalshi books list bids only, ascending, best bid last. Buying YES means taking
resting NO bids: a NO bid at price b offers YES at 1 - b (and the reverse).

`walk` is the shared book walk. `run` feeds observations to any `Strategy`
(see `pmw.plugins`) and records one row per entry; `settle` adds P&L once
outcomes are known. Paper only: nothing here talks to an exchange.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pmw.markets.fees import fill_fee

if TYPE_CHECKING:
    import pandas as pd

    from pmw.plugins import Strategy

TRADE_COLUMNS = [
    "ticker",
    "event_ticker",
    "t",
    "side",
    "contracts",
    "cost",
    "fees",
    "perceived",
    "q",
]


@dataclass
class Fill:
    price: float
    contracts: float
    fee: float


def walk(q: float, opp_px: list[float], opp_qty: list[float], tau: float, cap: float) -> list[Fill]:
    """Buy one side by taking the opposite side's resting bids, best (last) first.

    `q` is the model probability that the side bought wins; each opposite bid at
    price b offers this side at 1 - b. Levels are taken while the after-fee edge
    per contract at that level stays above tau, up to `cap` contracts.
    """
    fills: list[Fill] = []
    left = cap
    for px, qty in zip(reversed(opp_px), reversed(opp_qty), strict=True):
        if left <= 0:
            break
        price = round(1.0 - px, 4)
        n = math.floor(min(qty, left) * 100) / 100
        if n <= 0:
            continue
        fee = float(fill_fee(Decimal(str(price)), Decimal(str(n))).total)
        if q - price - fee / n <= tau:
            break
        fills.append(Fill(price, n, fee))
        left -= n
    return fills


@dataclass(frozen=True)
class Observation:
    """One market at one decision time: a probability and the book at that time."""

    ticker: str
    event_ticker: str
    t: Any
    p_yes: float
    mid: float
    yes_px: list[float]
    yes_qty: list[float]
    no_px: list[float]
    no_qty: list[float]


@dataclass
class Exposure:
    """What a backtest has already entered: markets, and contracts per event."""

    entered: set[str] = field(default_factory=set)
    event_contracts: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Entry:
    """A paper entry: one side of one market, filled level by level."""

    side: str
    q: float
    fills: list[Fill]

    @property
    def contracts(self) -> float:
        return sum(f.contracts for f in self.fills)

    @property
    def cost(self) -> float:
        return sum(f.price * f.contracts for f in self.fills)

    @property
    def fees(self) -> float:
        return sum(f.fee for f in self.fills)


def observations(obs: pd.DataFrame, prob_col: str = "p_model") -> list[Observation]:
    """Rows sorted by (t, ticker), the order a live strategy would see them."""
    out = []
    for r in obs.sort_values(["t", "ticker"]).to_dict("records"):
        out.append(Observation(
            ticker=r["ticker"], event_ticker=r["event_ticker"], t=r["t"],
            p_yes=float(r[prob_col]), mid=float(r["mid"]),
            yes_px=list(r["yes_px"]), yes_qty=list(r["yes_qty"]),
            no_px=list(r["no_px"]), no_qty=list(r["no_qty"]),
        ))  # fmt: skip
    return out


def run(obs: pd.DataFrame, strategy: Strategy, prob_col: str = "p_model") -> pd.DataFrame:
    """Every entry the strategy makes, held to settlement."""
    import pandas as pd

    exposure = Exposure()
    rows: list[dict[str, Any]] = []
    for o in observations(obs, prob_col):
        e = strategy.decide(o, exposure)
        if e is None or not e.fills:
            continue
        n = e.contracts
        rows.append({"ticker": o.ticker, "event_ticker": o.event_ticker, "t": o.t,
                     "side": e.side, "contracts": n, "cost": e.cost, "fees": e.fees,
                     "perceived": e.q - (e.cost + e.fees) / n, "q": e.q})  # fmt: skip
        exposure.entered.add(o.ticker)
        exposure.event_contracts[o.event_ticker] = (
            exposure.event_contracts.get(o.event_ticker, 0.0) + n
        )
    return pd.DataFrame(rows, columns=TRADE_COLUMNS)


def settle(trades: pd.DataFrame, outcome: dict[str, int]) -> pd.DataFrame:
    """P&L per entry once each market's YES outcome (1 or 0) is known."""
    import numpy as np

    t = trades.copy()
    y = t["ticker"].map(outcome).astype(float)
    win = np.where(t["side"] == "yes", y, 1 - y)
    t["pnl"] = win * t["contracts"] - t["cost"] - t["fees"]
    t["realized"] = t["pnl"] / t["contracts"]
    return t
