from copy import copy
from datetime import date
from decimal import Decimal as D
from fractions import Fraction as F

import pytest
from django.utils import timezone

from tseh.models import DomainState, Ingredient, Price, PriceApplication
from tseh.services.commands import create_ingredient, save_dish, save_preparation, stamp
from tseh.services.price_queries import history_chart, price_history, price_impact
from tseh.services.queries import catalog
from tseh.calculation.recipes import Item, Prep, Component

pytestmark = pytest.mark.django_db


def token():
    return stamp(DomainState.objects.get(pk=1))


def ingredient(price="2000"):
    key = create_ingredient(token(), {"name": "Мука", "kind": "ingredient", "unit": "kg", "loss": D(0),
        "supplier_id": None, "pack_size": D(1), "pack_price": D(price), "price_date": date(2026, 1, 1)},
        confirm_zero=D(price) == 0)
    return Ingredient.objects.select_related("current_price").get(pk=key)


def proposal(item, price, size="1"):
    return Price.objects.create(ingredient=item, pack_price=D(price), pack_size=D(size), price_date=date(2025, 1, 1))


def test_history_repeated_application_correction_exclusion_and_equal_times(state):
    item = ingredient()
    b = proposal(item, "2200")
    proposal(item, "9999")  # Never applied: not a point, regardless of price date.
    PriceApplication.objects.create(price=b)
    PriceApplication.objects.create(price=item.current_price)
    now = timezone.now()
    PriceApplication.objects.update(applied_at=now)
    ids = list(PriceApplication.objects.order_by("id").values_list("id", flat=True))
    rows = price_history(item)
    assert [r.id for r in rows] == ids
    assert [r.unit_price for r in rows] == [F(2000), F(2200), F(2000)]
    assert [r.change for r in rows] == [None, F(10), F(-100, 11)]
    assert [r.absolute_change for r in rows] == [None, F(200), F(-200)]
    corrected = copy(b)
    corrected.pack_price = D(2100)
    corrected.comment = "Исправлена опечатка"
    amended = price_history(item, overrides={b.pk: corrected})
    assert [r.change for r in amended] == [None, F(5), F(-100, 21)]
    assert amended[1].price.comment == "Исправлена опечатка"
    assert [r.applied_at for r in amended] == [now, now, now]
    assert [r.change for r in price_history(item, excluded={b.pk})] == [None, F(0)]
    assert [r.unit_price for r in price_history(item, overrides={item.current_price_id: corrected})] == [F(2100), F(2200), F(2100)]
    b.refresh_from_db()
    assert b.pack_price == D(2200) and PriceApplication.objects.count() == 3


def test_deleted_middle_reconnects_neighbors_and_normalized_pack_price(state):
    item = ingredient()
    b = proposal(item, "2200")
    c = proposal(item, "2100")
    PriceApplication.objects.create(price=b)
    PriceApplication.objects.create(price=c)
    assert [r.change for r in price_history(item, excluded={b.pk})] == [None, F(5)]
    same = proposal(item, "10000", "5")
    PriceApplication.objects.create(price=same)
    assert price_history(item)[-1].unit_price == F(2000)


def test_previous_zero_and_chart_exact_bounded_coordinates(state):
    item = ingredient("0")
    other = proposal(item, "2000")
    PriceApplication.objects.create(price=other)
    rows = price_history(item)
    assert rows[-1].change is None and rows[-1].absolute_change == F(2000)
    chart = history_chart(rows)
    assert chart["points"] == "40.000,200.000 620.000,20.000"
    assert (chart["minimum"], chart["maximum"]) == (F(0), F(2000))
    assert chart["markers"][1]["applied_at"] == rows[1].applied_at
    assert history_chart(rows[:1])["points"] == "330.000,110.000"
    assert history_chart([])["points"] == ""


def test_impact_cascades_keeps_zero_dependencies_and_does_not_write(state):
    item = ingredient("0")
    first = save_preparation(token(), {"name": "A", "output_quantity": D(2), "output_unit": "kg"},
        [{"ingredient_id": item.pk, "quantity": D(1), "unit": "kg"}])
    second = save_preparation(token(), {"name": "B", "output_quantity": D("0.5"), "output_unit": "kg"},
        [{"preparation_id": first, "quantity": D("0.5"), "unit": "kg"}])
    dish = save_dish(token(), {"name": "Блюдо", "base_price": D(1000), "green_override": D(20), "orange_override": D(40)},
        [{"preparation_id": second, "quantity": D(100), "unit": "g"}])
    proposed = copy(item.current_price)
    proposed.pack_price = D(3000)
    result = price_impact(item, proposed, D(10))
    assert [(r["before"], r["after"], r["unit"]) for r in result["preparations"]] == [(F(0), F(1500), "kg"), (F(0), F(1500), "kg")]
    row = result["dishes"][0]
    assert row["obj"].pk == dish and row["before"].raw == F(0) and row["after"].raw == F(150)
    assert row["after"].total == D("150.00")
    assert row["new_assessment"].effective == D("900.00")
    assert row["new_assessment"].ratio == F(50, 3)
    assert row["new_assessment"].zone == "green"
    item.current_price.refresh_from_db()
    assert item.current_price.pack_price == 0 and PriceApplication.objects.count() == 1
    # Both override APIs coexist, including a changed output size.
    graph = catalog(override={first: Prep(D(1), "kg", (Component("ingredient", item.pk, D(1), "kg"),))},
                    item_override={item.pk: Item("kg", D(0), D(3000), D(1))})
    assert graph.prep_prices[second] == F(3000)


def test_unused_price_impact_is_empty(state):
    item = ingredient()
    assert price_impact(item, item.current_price, D(0)) == {"preparations": [], "dishes": []}
