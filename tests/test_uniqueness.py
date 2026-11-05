from decimal import Decimal as D
from fractions import Fraction
from unittest.mock import patch

import pytest

from tseh.models import AuditEntry, Dish, DomainState, PreparationLine
from tseh.services.commands import save_dish
from tseh.services.uniqueness import dish_uniqueness, uniqueness_report
from tests.test_stage2_services import item, line, prep, token

pytestmark = pytest.mark.django_db


def dish(name, rows):
    return save_dish(token(), {"name": name, "base_price": D(100)}, rows)


def test_literal_two_unique_of_four_with_nested_duplicates_zero_and_packaging(state):
    common_a, common_b = item(), item()
    unique_a, zero = item(), item(price="0")
    packaging = item(kind="packaging", unit="pc")
    a = prep("A", [line(common_a), line(unique_a), line(zero)])
    b = prep("B", [line(a, prep=True), line(common_b), line(common_a)])
    main = dish("Основное", [line(b, prep=True), line(a, prep=True), line(common_a), line(zero), line(packaging, unit="pc")])
    other = dish("Другое", [line(common_a), line(common_b), line(packaging, unit="pc")])
    report = uniqueness_report()
    assert report.ingredient_ids_by_preparation[a] == frozenset({common_a, unique_a, zero})
    assert report.ingredient_ids_by_preparation[b] == frozenset({common_a, common_b, unique_a, zero})
    result = report.dishes[main]
    assert result.ingredient_ids == frozenset({common_a, common_b, unique_a, zero})
    assert result.unique_ids == frozenset({unique_a, zero})
    assert (result.unique_count, result.total_count, result.percentage) == (2, 4, Fraction(50))
    assert report.dishes[other].percentage == Fraction(0)


def test_identical_names_are_distinct_uuids_and_quantities_do_not_weight(state):
    # The fixture deliberately gives every ingredient the same name "X".
    first, second = item(), item()
    a = dish("A", [line(first, "0.0001"), line(first, "100")])
    b = dish("B", [line(second)])
    result = dish_uniqueness()
    assert result[a].ingredient_ids == frozenset({first})
    assert result[b].ingredient_ids == frozenset({second})
    assert result[a].percentage == result[b].percentage == Fraction(100)


def test_empty_and_packaging_only_have_undefined_percentage(state):
    packaging = item(kind="packaging", unit="pc")
    packed = dish("Только упаковка", [line(packaging, unit="pc")])
    empty = Dish.objects.create(name="Пустая запись", base_price=D(1))
    results = dish_uniqueness()
    for key in [packed, empty.pk]:
        assert results[key].ingredient_ids == results[key].unique_ids == frozenset()
        assert results[key].total_count == results[key].unique_count == 0
        assert results[key].percentage is None


def test_changing_another_dish_recomputes_global_uniqueness(state):
    a, b, c = item(), item(), item()
    first = dish("Visible", [line(a), line(b), line(c)])
    hidden = dish("Outside current filter/page", [line(a)])
    assert dish_uniqueness()[first].percentage == Fraction(200, 3)
    save_dish(token(), {"name": "Outside current filter/page", "base_price": D(100)}, [line(a), line(b)], hidden)
    result = dish_uniqueness()[first]
    assert result.unique_ids == frozenset({c}) and result.percentage == Fraction(100, 3)


def test_five_queries_no_financial_dependency_or_audit_write(state, django_assert_num_queries):
    ingredient = item()
    a = prep("A", [line(ingredient)])
    for index in range(35):
        dish(f"Dish {index}", [line(a, prep=True)])
    before = DomainState.objects.get(pk=1).revision, AuditEntry.objects.count()
    with patch("tseh.services.queries.catalog", side_effect=AssertionError("Financial calculator must not be used")):
        with django_assert_num_queries(5):
            report = uniqueness_report()
            assert len(report.dishes) == 35
            assert report.ingredient_ids_by_preparation[a] == frozenset({ingredient})
            assert all(result.percentage == 0 for result in report.dishes.values())
    assert (DomainState.objects.get(pk=1).revision, AuditEntry.objects.count()) == before


def test_invalid_cycle_fails_explicitly_without_recursion_or_silent_counts(state):
    ingredient = item()
    a = prep("A", [line(ingredient)])
    b = prep("B", [line(a, prep=True)])
    # Bypass command validation only in the isolated test DB to simulate damaged data.
    PreparationLine.objects.filter(parent_id=a).update(ingredient_id=None, preparation_id=b)
    with pytest.raises(ValueError, match="Цикл"):
        uniqueness_report()


def test_empty_catalog_is_valid_with_fixed_query_budget(state, django_assert_num_queries):
    with django_assert_num_queries(5):
        report = uniqueness_report()
    assert report.dishes == {} and report.ingredient_ids_by_preparation == {}
