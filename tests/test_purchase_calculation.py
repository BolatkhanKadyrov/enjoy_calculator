from decimal import Decimal as D, localcontext
from fractions import Fraction as F
from types import SimpleNamespace

import pytest

from tseh.calculation.purchases import aggregate_lines, allocate_delivery, exact_decimal


def row(position, price="1", size="1", count="1", key=None, share="0"):
    return SimpleNamespace(id=position, position=position, ingredient_id=key or position,
        pack_price=D(price), pack_size=D(size), pack_count=D(count) if count else None,
        delivery_share=D(share))


@pytest.mark.parametrize("total,expected", [("1", [".34", ".33", ".33"]), ("2", [".67", ".67", ".66"])])
def test_largest_remainder_keeps_cents_and_stable_order(total, expected):
    rows = [row(index) for index in range(3)]
    assert allocate_delivery(D(total), list(reversed(rows))) == dict(enumerate(map(D, expected)))


def test_largest_remainder_priority_is_fraction_not_position():
    rows = [row(0), row(1, "2"), row(2, "3")]
    assert allocate_delivery(D(".01"), rows) == {0: D(0), 1: D(0), 2: D(".01")}


def test_duplicate_weighted_total_uses_saved_delivery_before_grouping():
    rows = [row(0, "1000", count="2", key="A"), row(1, "3000", "2", "4", "A"), row(2, "2000", "2", "3", "B")]
    shares = allocate_delivery(D(1000), rows)
    assert list(shares.values()) == [D(100), D(600), D(300)]
    for line in rows:
        line.delivery_share = shares[line.id]
    totals = aggregate_lines(rows)
    assert totals["A"]["unit_price"] == 1470
    assert totals["B"]["unit_price"] == 1050
    assert totals["A"]["goods_total"] == 14000 and totals["A"]["quantity_total"] == 10


def test_allocation_and_aggregate_independent_of_decimal_context():
    line = row(0, "999999999999.99", "999999999999.999999999999", "999999999999.999999999999")
    with localcontext() as context:
        context.prec = 6
        result = aggregate_lines([line])[0]
        assert F(result["quantity_total"]) == F(line.pack_size) * F(line.pack_count)
        assert F(result["goods_total"]) == F(line.pack_price) * F(line.pack_count)
        assert exact_decimal(F(1, 2 ** 20 * 5 ** 30)) == D("1.024E-27")


def test_missing_quantity_only_allowed_for_single_undelivered_offer():
    lines = [row(0, "100", "3", count=None, key="A")]
    assert aggregate_lines(lines)["A"]["unit_price"] == F(100, 3)
    assert allocate_delivery(D(0), lines) == {0: D(0)}
    with pytest.raises(ValueError, match="количество"):
        allocate_delivery(D(1), lines)
    with pytest.raises(ValueError, match="количество"):
        aggregate_lines(lines + [row(1, "200", "3", "2", "A")])
    with pytest.raises(ValueError, match="больше нуля"):
        allocate_delivery(D(1), [row(0, "0")])
