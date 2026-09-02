"""The rounding convention, and the refusal to touch binary float."""
from decimal import Decimal

import pytest

from praman.ledger.money import (apply_rate, dec, invert_legs, leg, legs_balance,
                                 money, money_str, rate)


def test_rounds_half_away_from_zero_not_bankers():
    # Python's round() would give 6.32 here. The gateway's statement says 6.33.
    assert money("6.325") == Decimal("6.33")
    assert money("-1.005") == Decimal("-1.01")
    assert money("2.675") == Decimal("2.68")


def test_dec_refuses_nothing_but_warns_on_float(caplog):
    assert dec("1799.00") == Decimal("1799.00")
    assert dec(1799) == Decimal("1799")
    with caplog.at_level("WARNING"):
        dec(0.1)
    assert "float reached dec()" in caplog.text


def test_dec_rejects_unparseable_types():
    with pytest.raises(TypeError):
        dec(None)
    with pytest.raises(TypeError):
        dec({"amount": "1.00"})


def test_apply_rate_rounds_exactly_once():
    # 1799 * 2% = 35.98 exactly; 35.98 * 18% = 6.4764 -> 6.48
    assert apply_rate("1799.00", "0.0200") == Decimal("35.98")
    assert apply_rate("35.98", "0.1800") == Decimal("6.48")


def test_money_str_is_always_two_places():
    assert money_str(0) == "0.00"
    assert money_str("1E+2") == "100.00"
    assert money_str(Decimal("0E-2")) == "0.00"


def test_legs_balance_and_invert():
    legs = [leg("1200", debit="100.00"), leg("4000", credit="100.00")]
    assert legs_balance(legs)
    assert legs_balance([])
    inv = invert_legs(legs)
    assert inv[0]["credit"] == "100.00" and inv[0]["debit"] == "0.00"
    assert legs_balance(legs + inv)


def test_rate_normalises_to_four_places():
    assert rate("0.02") == Decimal("0.0200")
    assert rate(".001") == Decimal("0.0010")
