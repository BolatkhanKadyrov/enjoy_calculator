from decimal import Decimal as D, localcontext
from fractions import Fraction

import pytest

from tseh.calculation import calculate, line_cost, money, normalized, unit_price


def row(**changes):
    data = dict(quantity=D("100"), unit="g", base_unit="kg", loss=D("25"), pack_price=D("10000"), pack_size=D("5"))
    data.update(changes)
    return data


@pytest.mark.parametrize("source,expected", [("12.344", "12.34"), ("12.345", "12.35"), ("12.346", "12.35"),
    ("1.005", "1.01"), ("-1.005", "-1.01"), ("0.005", "0.01"), ("0", "0.00")])
def test_literal_rounding_examples(source, expected):
    assert money(D(source)) == D(expected)


def test_pack_and_primary_losses_acceptance_01():
    assert unit_price(D("10000"), D("5")) == 2000
    assert line_cost(**row()) == Fraction(800, 3)
    assert calculate([row()]).total == D("266.67")
    assert calculate([row(loss=D(0))]).total == D("200.00")


def test_sum_before_rounding_acceptance_07():
    component = row(quantity=D("10.004"), unit="kg", loss=D(0), pack_price=D(1), pack_size=D(1))
    result = calculate([component, component, component])
    assert result.raw == Fraction(7503, 250)
    assert result.total == D("30.01")


def test_repeating_division_exact_half_cent_independent_literal():
    # Hand calculation: .01 * 16.5 / 3 = .055. Never obtain the expected value from the core.
    component = row(quantity=D("16.5"), unit="pc", base_unit="pc", loss=D(0), pack_price=D("0.01"), pack_size=D(3))
    assert calculate([component]).total == D("0.06")


def test_small_intermediate_unit_price_acceptance_02():
    component = row(quantity=D(1000), unit="pc", base_unit="pc", loss=D(0), pack_price=D(1), pack_size=D(250))
    assert unit_price(D(1), D(250)) == Fraction(1, 250)
    assert calculate([component]).total == D("4.00")


@pytest.mark.parametrize("small,large,base", [("g", "kg", "kg"), ("ml", "l", "l")])
def test_equivalent_units(small, large, base):
    assert normalized(D(100), small, base) == normalized(D("0.1"), large, base) == Fraction(1, 10)


@pytest.mark.parametrize("loss", [D(-1), D(100), D(110)])
def test_invalid_losses(loss):
    with pytest.raises(ValueError):
        line_cost(**row(loss=loss))


def test_no_mass_volume_conversion():
    with pytest.raises(ValueError):
        normalized(D(1), "l", "kg")


@pytest.mark.parametrize("size,price", [(D(0), D(1)), (D(-1), D(1)), (D(1), D(-1))])
def test_invalid_price_input(size, price):
    with pytest.raises(ValueError):
        unit_price(price, size)


def test_rounding_does_not_depend_on_decimal_context():
    with localcontext() as ctx:
        ctx.prec = 3
        assert money(Fraction(123456789005, 1000)) == D("123456789.01")
