"""Fee tests. Vectors marked DOCS come straight from Kalshi's API documentation."""

from decimal import Decimal as D

import pytest

from pmw.markets.fees import (
    NON_DIRECT_MEMBER_PRECISION,
    fee_per_contract,
    fill_fee,
    model_fee,
    taker_ev_buy_no,
    taker_ev_buy_yes,
)


def test_docs_worked_example_model_fee():
    # DOCS "Fee Rounding": a buy with -$0.055 revenue has model fee $0.00363825.
    assert model_fee("0.055", 1) == D("0.00363825")


def test_docs_worked_example_rounding_non_direct_member():
    # DOCS: trade fee $0.003639, rounding fee $0.001361, balance change -$0.06,
    # trade fee + rounding fee exactly $0.005.
    f = fill_fee("0.055", 1, balance_precision=NON_DIRECT_MEMBER_PRECISION)
    assert f.trade_fee == D("0.003639")
    assert f.rounding_fee == D("0.001361")
    assert f.balance_change == D("-0.06")
    assert f.total == D("0.005")


def test_direct_member_rounds_to_hundredth_of_a_cent():
    f = fill_fee("0.055", 1)
    assert f.trade_fee == D("0.003639")
    assert f.balance_change == D("-0.0587")
    assert f.total == D("0.0037")


def test_fee_is_symmetric_and_peaks_at_fifty_cents():
    assert model_fee("0.30", 100) == model_fee("0.70", 100) == D("1.47")
    assert model_fee("0.50", 100) == D("1.75")
    assert model_fee("0.50", 100) > model_fee("0.49", 100)


def test_fee_is_zero_at_the_extremes():
    assert model_fee(0, 10) == 0
    assert model_fee(1, 10) == 0


def test_fee_multiplier_scales_linearly():
    assert model_fee("0.4", 10, fee_multiplier="0.5") == model_fee("0.4", 10) / 2
    assert model_fee("0.4", 10, fee_multiplier=0) == 0


def test_maker_fees_by_fee_type():
    # 'quadratic' series charge makers nothing; the maker-fee types charge a
    # fraction of the taker rate (0.25 and 0.5 per the API docs).
    assert model_fee("0.5", 100, is_taker=False) == 0
    assert model_fee("0.5", 100, is_taker=False, fee_type="quadratic_with_maker_fees") == D(
        "0.4375"
    )
    assert model_fee("0.5", 100, is_taker=False, fee_type="quadratic_with_combo_maker_fees") == D(
        "0.875"
    )


def test_selling_floors_proceeds_in_the_exchange_favour():
    f = fill_fee("0.055", 1, is_buy=False, balance_precision=NON_DIRECT_MEMBER_PRECISION)
    assert f.balance_change == D("0.05")
    assert f.total == D("0.005")


def test_fractional_contracts():
    f = fill_fee("0.56", "7.40")
    assert f.trade_fee == D("0.127636")  # ceil(0.07 * 7.4 * 0.56 * 0.44) to 6 dp
    assert f.total >= f.trade_fee


def test_per_contract_fee_and_ev():
    per = fee_per_contract("0.59", 100)
    assert per == D("0.016933")  # 0.07 * 0.59 * 0.41, already on the 0.0001 grid for 100 lots
    assert taker_ev_buy_yes("0.70", "0.59", 100) == D("0.70") - D("0.59") - per
    # Buying NO at 1 - bid: a model of 0.40 against a 0.56 bid.
    ev_no = taker_ev_buy_no("0.40", "0.56", 100)
    assert ev_no == D("0.60") - D("0.44") - fee_per_contract("0.44", 100)


@pytest.mark.parametrize("bad", ["-0.01", "1.01"])
def test_rejects_prices_outside_unit_interval(bad):
    with pytest.raises(ValueError):
        model_fee(bad, 1)


def test_rejects_unknown_fee_type():
    with pytest.raises(ValueError):
        model_fee("0.5", 1, fee_type="flat")
