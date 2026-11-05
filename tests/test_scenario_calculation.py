"""Independent literal oracles for the pure multi-price comparison kernel."""
from decimal import Decimal as D, localcontext
from fractions import Fraction as F

from tseh.calculation.recipes import Component as C, Item, Prep, RecipeGraph
from tseh.calculation.scenarios import compare_recipe


def simple_graph(prices):
    return RecipeGraph({key: Item("kg", D(0), D(price), D(1))
                        for key, price in prices.items()}, {})


def test_combined_changes_and_removed_oil_acceptance_18():
    lines = (C("ingredient", "chicken", D(".1"), "kg"),
             C("ingredient", "rice", D(".1"), "kg"),
             C("ingredient", "oil", D(".01"), "l"))
    def graph(chicken, rice, oil):
        return RecipeGraph({"chicken": Item("kg", D(0), D(chicken), D(1)),
            "rice": Item("kg", D(0), D(rice), D(1)),
            "oil": Item("l", D(0), D(oil), D(1))}, {})
    before = graph("2000", "600", "900")
    full = compare_recipe(before, graph("2300", "650", "1100"), lines)
    assert (full.before.total, full.after.total, full.delta) == (D(269), D(306), D(37))
    assert full.percent == F(3700, 269)
    assert {row.key: row.raw_delta for row in full.contributions} == {
        "chicken": F(30), "rice": F(5), "oil": F(2)}
    without_oil = compare_recipe(before, graph("2300", "650", "900"), lines)
    assert (without_oil.after.total, without_oil.delta, without_oil.percent) == (
        D(304), D(35), F(3500, 269))
    assert next(row for row in without_oil.contributions if row.key == "oil").raw_delta == 0
    assert not full.has_rounding and not without_oil.has_rounding


def test_nested_shared_ingredient_and_packaging_are_counted_once():
    def graph(price, box):
        return RecipeGraph({"X": Item("kg", D(0), D(price), D(1)),
            "box": Item("pc", D(0), D(box), D(1), "packaging")}, {
            "A": Prep(D(1), "kg", (C("ingredient", "X", D(1), "kg"),)),
            "B": Prep(D(".5"), "kg", (C("preparation", "A", D(".5"), "kg"),)),
        })
    lines = (C("ingredient", "X", D(".1"), "kg"),
             C("preparation", "A", D(".2"), "kg"),
             C("preparation", "B", D(".3"), "kg"),
             C("ingredient", "box", D(1), "pc"))
    result = compare_recipe(graph("1000", "50"), graph("1200", "60"), lines)
    assert (result.before.total, result.after.total, result.delta, result.percent) == (
        D(650), D(780), D(130), F(20))
    assert {row.key: (row.before, row.after, row.raw_delta) for row in result.contributions} == {
        "X": (F(600), F(720), F(120)), "box": (F(50), F(60), F(10))}
    assert [row.raw_delta for row in result.branches] == [F(20), F(40), F(60), F(10)]
    assert [row.component for row in result.branches] == list(lines)
    assert [row.position for row in result.branches] == [0, 1, 2, 3]
    assert [row.contributions[0].raw_delta for row in result.branches] == [F(20), F(40), F(60), F(10)]
    assert result.raw_adjustment == 0 and result.display_adjustment == D("0.00")


def test_three_independent_rounding_cases_have_literal_adjustments():
    # Each ingredient is distinct, so the visible summary contains each delta.
    cases = [
        (2, "1.004", "1.006", "2.01", "2.01", "0.00", ".002", "0.00", "-.004", "0.00"),
        (3, "1.000", "1.004", "3.00", "3.01", ".01", ".004", "0.00", "-.002", ".01"),
        (2, "1.004", "1.009", "2.01", "2.02", ".01", ".005", ".01", "0", "-.01"),
    ]
    for count, old, new, old_total, new_total, delta, leaf_delta, displayed, raw_adj, display_adj in cases:
        keys = [f"item-{index}" for index in range(count)]
        lines = tuple(C("ingredient", key, D(1), "kg") for key in keys)
        result = compare_recipe(simple_graph(dict.fromkeys(keys, old)),
                                simple_graph(dict.fromkeys(keys, new)), lines)
        assert (result.before.total, result.after.total, result.delta) == (
            D(old_total), D(new_total), D(delta))
        assert [row.raw_delta for row in result.contributions] == [F(leaf_delta)] * count
        assert [row.display_delta for row in result.contributions] == [D(displayed)] * count
        assert result.raw_adjustment == F(raw_adj)
        assert result.display_adjustment == D(display_adj)
        assert result.has_rounding


def test_aggregate_repeated_leaf_before_rounding_its_visible_delta():
    lines = tuple(C("ingredient", "X", D(1), "kg") for _ in range(3))
    result = compare_recipe(simple_graph({"X": "1.000"}), simple_graph({"X": "1.004"}), lines)
    assert result.delta == D(".01")
    assert len(result.contributions) == 1
    assert result.contributions[0].raw_delta == F(".012")
    assert result.contributions[0].display_delta == D(".01")
    assert [row.display_delta for row in result.branches] == [D("0.00")] * 3
    assert result.raw_adjustment == F("-.002")
    assert result.display_adjustment == D("0.00")
    assert result.has_rounding


def test_negative_changes_and_half_cent_costs_use_final_totals():
    lines = (C("ingredient", "X", D("16.5"), "pc"),)
    before = RecipeGraph({"X": Item("pc", D(0), D(".02"), D(3))}, {})
    after = RecipeGraph({"X": Item("pc", D(0), D(".01"), D(3))}, {})
    result = compare_recipe(before, after, lines)
    assert (result.before.raw, result.after.raw) == (F(".11"), F(".055"))
    assert (result.before.total, result.after.total, result.delta) == (D(".11"), D(".06"), D("-.05"))
    assert result.percent == F(-500, 11)
    assert result.contributions[0].raw_delta == F("-.055")
    assert result.contributions[0].display_delta == D("-.06")
    assert result.raw_adjustment == F(".005")
    assert result.display_adjustment == D(".01")


def test_zero_base_percentage_is_undefined_even_when_unchanged():
    lines = (C("ingredient", "X", D(1), "kg"),)
    zero = simple_graph({"X": "0"})
    result = compare_recipe(zero, simple_graph({"X": "1"}), lines)
    assert result.delta == D("1.00") and result.percent is None
    unchanged = compare_recipe(zero, zero, lines)
    assert unchanged.delta == D("0.00") and unchanged.percent is None
    assert len(unchanged.contributions) == 1 and not unchanged.has_rounding


def test_purchase_effective_prices_and_primary_losses_remain_exact():
    # Source purchase A has the independently established weighted price 1470.
    before = RecipeGraph({"A": Item("kg", D(20), None, None, effective_price=F(1400)),
                          "B": Item("kg", D(0), None, None, effective_price=F(900))}, {})
    after = RecipeGraph({"A": Item("kg", D(20), None, None, effective_price=F(1470)),
                         "B": Item("kg", D(0), None, None, effective_price=F(900))}, {})
    lines = (C("ingredient", "A", D(".1"), "kg"), C("ingredient", "B", D(".1"), "kg"))
    result = compare_recipe(before, after, lines)
    assert (result.before.total, result.after.total, result.delta) == (D(265), D("273.75"), D("8.75"))
    assert result.percent == F(175, 53)
    assert [row.raw_delta for row in result.contributions] == [F("8.75"), F(0)]


def test_generator_recipe_preserves_branch_order_and_stable_leaf_order():
    lines = [C("ingredient", "z", D(1), "kg"), C("ingredient", "a", D(2), "kg")]
    result = compare_recipe(simple_graph({"a": "1", "z": "2"}),
                            simple_graph({"a": "2", "z": "4"}), iter(lines))
    assert [row.key for row in result.contributions] == ["a", "z"]
    assert [row.component.key for row in result.branches] == ["z", "a"]
    assert (result.before.total, result.after.total, result.delta) == (D(4), D(8), D(4))


def test_low_decimal_context_does_not_round_totals_difference_or_percent():
    before = simple_graph({"X": "123456789.01"})
    after = simple_graph({"X": "123456789.02"})
    with localcontext() as context:
        context.prec = 3
        result = compare_recipe(before, after, (C("ingredient", "X", D(3), "kg"),))
    assert (result.before.total, result.after.total, result.delta) == (
        D("370370367.03"), D("370370367.06"), D(".03"))
    assert result.percent == F(300, 37037036703)
    assert result.contributions[0].display_delta == D(".03")
    assert not result.has_rounding


def test_shared_deep_graph_uses_aggregated_leaves_not_exponential_paths():
    preparations = {"prep-0": Prep(D(1), "kg", (C("ingredient", "X", D(1), "kg"),))}
    for level in range(1, 36):
        previous = C("preparation", f"prep-{level - 1}", D(1), "kg")
        preparations[f"prep-{level}"] = Prep(D(2), "kg", (previous, previous))
    before = RecipeGraph({"X": Item("kg", D(0), D(10), D(1))}, preparations)
    after = RecipeGraph({"X": Item("kg", D(0), D(15), D(1))}, preparations)
    result = compare_recipe(before, after, (C("preparation", "prep-35", D(2), "kg"),))
    assert (result.before.total, result.after.total, result.delta) == (D(20), D(30), D(10))
    assert len(result.contributions) == 1 and len(result.branches[0].contributions) == 1
    assert result.contributions[0].raw_delta == F(10)
