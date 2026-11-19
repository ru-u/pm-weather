"""Kalshi trading fees.

Sources (checked 2026-11-14), see docs/data_sources.md:
  * API docs, Series.fee_type: 'quadratic' is the General Trading Fees table;
    'quadratic_with_maker_fees' adds maker fees with a 0.25 maker multiplier;
    'quadratic_with_combo_maker_fees' uses a 0.5 maker multiplier. Each series
    also carries a `fee_multiplier` "applied to the fee calculations".
  * API docs, "Fee Rounding": the trade fee is the model fee rounded UP to
    $0.000001, then a rounding fee floors the balance change to the member's
    balance precision ($0.0001 for direct members, $0.01 for non-direct).
  * The 0.07 taker coefficient: the docs' worked example (a buy with $0.055 of
    revenue and a model fee of $0.00363825) equals 0.07 * 1 * 0.055 * 0.945
    exactly. That is the published General Trading Fees formula
    fee = 0.07 * C * P * (1 - P).

Reconciled against a real fill on 2026-11-19 (D-057): 1 YES at $0.70 on
KXTXERCOTPEAKD, taker. Kalshi's fill `fee_cost` was $0.014700, equal to
`fill_fee(0.70, 1).trade_fee` (0.07 x 0.70 x 0.30, not rounded up to the cent),
and cash moved by exactly $0.7147. Maker fees (MAKER_FRACTION) are still "per
API docs": the fee-schedule PDF could not be fetched automatically and no maker
fill has been reconciled.

Everything here is exact Decimal arithmetic. Prices are dollars per contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

TAKER_RATE = Decimal("0.07")
MAKER_FRACTION: dict[str, Decimal] = {
    "quadratic": Decimal("0"),
    "quadratic_with_maker_fees": Decimal("0.25"),
    "quadratic_with_combo_maker_fees": Decimal("0.5"),
}
FEE_GRAIN = Decimal("0.000001")
DIRECT_MEMBER_PRECISION = Decimal("0.0001")
NON_DIRECT_MEMBER_PRECISION = Decimal("0.01")

Number = Decimal | float | int | str


def _d(x: Number) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _floor_to(x: Decimal, grain: Decimal) -> Decimal:
    return (x / grain).to_integral_value(rounding=ROUND_FLOOR) * grain


def _ceil_to(x: Decimal, grain: Decimal) -> Decimal:
    return (x / grain).to_integral_value(rounding=ROUND_CEILING) * grain


def model_fee(
    price: Number,
    count: Number,
    *,
    fee_type: str = "quadratic",
    fee_multiplier: Number = 1,
    is_taker: bool = True,
) -> Decimal:
    """Unrounded fee from the fee model for one fill of `count` contracts at `price`."""
    p, c = _d(price), _d(count)
    if not (Decimal(0) <= p <= Decimal(1)):
        raise ValueError(f"price must be in [0, 1] dollars, got {price}")
    if c < 0:
        raise ValueError("count must be non-negative")
    if fee_type not in MAKER_FRACTION:
        raise ValueError(f"unsupported fee_type {fee_type!r}")
    rate = TAKER_RATE if is_taker else TAKER_RATE * MAKER_FRACTION[fee_type]
    return rate * _d(fee_multiplier) * c * p * (Decimal(1) - p)


@dataclass(frozen=True)
class FillFee:
    trade_fee: Decimal  # model fee rounded up to $0.000001
    rounding_fee: Decimal  # brings the balance change onto the precision grid
    balance_change: Decimal  # signed; what actually moves in the account

    @property
    def total(self) -> Decimal:
        """Total fee for this fill before any accumulator rebate (an upper bound)."""
        return self.trade_fee + self.rounding_fee


def fill_fee(
    price: Number,
    count: Number,
    *,
    is_buy: bool = True,
    fee_type: str = "quadratic",
    fee_multiplier: Number = 1,
    is_taker: bool = True,
    balance_precision: Decimal = DIRECT_MEMBER_PRECISION,
) -> FillFee:
    """Fee for one fill, following the docs' rounding mechanics step by step.

    `price` is the price of the side being bought or sold. Rebates from the
    per-order rounding accumulator are not modelled, so `total` is a slight
    upper bound for orders that fill in several pieces.
    """
    p, c = _d(price), _d(count)
    revenue = -(p * c) if is_buy else p * c
    trade_fee = _ceil_to(
        model_fee(p, c, fee_type=fee_type, fee_multiplier=fee_multiplier, is_taker=is_taker),
        FEE_GRAIN,
    )
    aligned_change = _floor_to(revenue - trade_fee, balance_precision)
    rounding_fee = (revenue - trade_fee) - aligned_change
    return FillFee(trade_fee=trade_fee, rounding_fee=rounding_fee, balance_change=aligned_change)


def fee_per_contract(
    price: Number,
    count: Number = 1,
    *,
    fee_type: str = "quadratic",
    fee_multiplier: Number = 1,
    is_taker: bool = True,
    balance_precision: Decimal = DIRECT_MEMBER_PRECISION,
) -> Decimal:
    """Total fee per contract for a buy of `count` contracts at `price`."""
    c = _d(count)
    if c <= 0:
        raise ValueError("count must be positive")
    fee = fill_fee(
        price,
        c,
        fee_type=fee_type,
        fee_multiplier=fee_multiplier,
        is_taker=is_taker,
        balance_precision=balance_precision,
    )
    return fee.total / c


def taker_ev_buy_yes(
    p_model: Number, yes_ask: Number, count: Number = 1, **fee_kwargs: object
) -> Decimal:
    """Expected value per contract of lifting the YES ask: p - ask - fee(ask)."""
    return _d(p_model) - _d(yes_ask) - fee_per_contract(yes_ask, count, **fee_kwargs)  # type: ignore[arg-type]


def taker_ev_buy_no(
    p_model: Number, yes_bid: Number, count: Number = 1, **fee_kwargs: object
) -> Decimal:
    """EV per contract of hitting the YES bid, i.e. buying NO at (1 - yes_bid)."""
    no_price = Decimal(1) - _d(yes_bid)
    return (Decimal(1) - _d(p_model)) - no_price - fee_per_contract(no_price, count, **fee_kwargs)  # type: ignore[arg-type]
