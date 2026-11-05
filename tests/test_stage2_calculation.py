from decimal import Decimal as D, ROUND_HALF_UP, localcontext
from fractions import Fraction as F

import pytest

from tseh.calculation import money
from tseh.calculation.pricing import assess, minimum_price
from tseh.calculation.recipes import Component as C, Item, Prep, RecipeGraph


def sample_graph(price="1000", loss="0"):
    return RecipeGraph({"X": Item("kg", D(loss), D(price), D(1))}, {
        "A": Prep(D(1), "kg", (C("ingredient", "X", D(1), "kg"),)),
        "B": Prep(D("0.5"), "kg", (C("preparation", "A", D("0.5"), "kg"),))})


def recipe():
    return (C("ingredient", "X", D("0.1"), "kg"), C("preparation", "A", D("0.2"), "kg"), C("preparation", "B", D("0.3"), "kg"))


def test_nested_repeated_leaf_acceptance_05():
    before, after = sample_graph(), sample_graph("1200")
    assert before.calculate(recipe()).total == D("600.00")
    assert after.calculate(recipe()).total == D("720.00")
    assert tuple(new - old for new, old in zip(after.calculate(recipe()).rows, before.calculate(recipe()).rows)) == (20, 40, 60)
    assert after.contributions(recipe())["X"] - before.contributions(recipe())["X"] == 120
    assert sample_graph(loss="20").calculate(recipe()).total == D("750.00")


def test_output_not_input_mass_acceptance_04():
    items = {"X": Item("kg", D(0), D(3000), D(1))}
    before = RecipeGraph(items, {"A": Prep(D(2), "kg", (C("ingredient", "X", D(1), "kg"),))})
    after = RecipeGraph(items, {"A": Prep(D("1.5"), "kg", (C("ingredient", "X", D(1), "kg"),))})
    assert before.calculate((C("preparation", "A", D(100), "g"),)).total == D("150.00")
    assert after.calculate((C("preparation", "A", D(100), "g"),)).total == D("200.00")


def test_preparation_intermediate_never_rounded():
    graph = RecipeGraph({"X": Item("pc", D(0), D("0.01"), D(3))}, {"A": Prep(D(1), "pc", (C("ingredient", "X", D("16.5"), "pc"),))})
    assert graph.prep_costs["A"].raw == F(11, 200)
    assert graph.prep_costs["A"].total == D("0.06")  # display only
    assert graph.calculate((C("preparation", "A", D(2), "pc"),)).total == D("0.11")


def test_sum_before_rounding_through_preparation_acceptance_07():
    graph = RecipeGraph({"X": Item("pc", D(0), D(2501), D(250))}, {"A": Prep(D(1), "pc", (C("ingredient", "X", D(1), "pc"),))})
    assert graph.calculate(tuple(C("preparation", "A", D(1), "pc") for _ in range(3))).total == D("30.01")


def test_packaging_acceptance_06():
    rows = (C("ingredient", "X", D(600), "g"), C("ingredient", "box", D(1), "pc"))
    items = {"X": Item("kg", D(0), D(1000), D(1)), "box": Item("pc", D(0), D(25000), D(500), "packaging")}
    assert RecipeGraph(items, {}).calculate(rows).total == D("650.00")
    items["box"] = Item("pc", D(0), D(30000), D(500), "packaging")
    assert RecipeGraph(items, {}).calculate(rows).total == D("660.00")


def test_cycles_and_zero_output_rejected():
    with pytest.raises(ValueError, match="Цикл"):
        RecipeGraph({}, {"A": Prep(D(1), "kg", (C("preparation", "B", D(1), "kg"),)), "B": Prep(D(1), "kg", (C("preparation", "A", D(1), "kg"),))})
    with pytest.raises(ValueError):
        RecipeGraph({"X": Item("kg", D(0), D(1), D(1))}, {"A": Prep(D(0), "kg", (C("ingredient", "X", D(1), "kg"),))})


@pytest.mark.parametrize("cost,zone", [("400", "green"), ("400.01", "orange"), ("600", "orange"), ("600.01", "red"), ("400.04", "orange"), ("600.04", "red")])
def test_exact_zone_boundaries_acceptance_09(cost, zone):
    assert assess(D(cost), D(1000), D(0), D(40), D(60)).zone == zone


def test_money_basis_and_discount_acceptance_08():
    result = assess(D("400.00"), D(1000), D(0), D(40), D(60))
    assert result.zone == "green"
    result = assess(D(120), D("333.33"), D(10), D(40), D(60))
    assert result.effective == D("300.00")
    assert result.ratio == 40
    assert result.zone == "green"
    assert assess(D(700), D(1945), D(10)).effective == D("1750.50")
    result = assess(D(700), D(1400), D(10))
    assert result.effective == 1260
    assert result.ratio == F(500, 9)
    result = assess(D(1), D("0.01"), D(90), D(40), D(60))
    assert result.effective == 0 and result.ratio is None and result.zone == "no_price"


def test_high_thresholds_allowed_and_invalid_pair_has_no_minimum():
    assert assess(D(1100), D(1000), D(0), D(90), D(120)).zone == "orange"
    result = assess(D(400), D(1000), D(0), D(55), D(50))
    assert result.zone == "invalid" and result.ratio == 40 and result.minimum is None
    assert assess(D(400), D(1000), D(0)).zone == "unconfigured"
    assert minimum_price(D(400), D(0), None) is None


@pytest.mark.parametrize("cost,expected,previous_effective", [("120", "333.33", "299.99"), ("700", "1944.44", "1749.99")])
def test_minimum_literal_examples_acceptance_10(cost, expected, previous_effective):
    result = minimum_price(D(cost), D(10), D(40), D(60))
    assert result == D(expected)
    assert assess(D(cost), result, D(10), D(40), D(60)).zone == "green"
    lower = assess(D(cost), result - D("0.01"), D(10), D(40), D(60))
    assert lower.effective == D(previous_effective) and lower.zone != "green"


@pytest.mark.parametrize("cost", ["0", "0.01", "0.99"])
@pytest.mark.parametrize("discount", ["0", "50", "90"])
@pytest.mark.parametrize("green", ["40", "100"])
def test_minimum_against_independent_exhaustive_decimal_oracle(cost, discount, green):
    # Independent enumeration and Decimal quantize, not the production binary search/money function.
    with localcontext() as ctx:
        ctx.prec = 70
        expected = None
        for cents in range(1, 2501):
            price = D(cents) / 100
            effective = (price * (1 - D(discount) / 100)).quantize(D("0.01"), rounding=ROUND_HALF_UP)
            if effective > 0 and D(cost) * 100 <= D(green) * effective:
                expected = price
                break
    assert expected is not None
    assert minimum_price(D(cost), D(discount), D(green)) == expected
