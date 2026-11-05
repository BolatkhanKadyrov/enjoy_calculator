from datetime import date
from decimal import Decimal as D
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError

from tseh.models import AuditEntry, Dish, DomainState, GlobalSettings, Ingredient, Preparation
from tseh.services.commands import create_ingredient, save_dish, save_preparation, save_supplier, save_thresholds, stamp, StaleWrite
from tseh.services.queries import catalog, dish_calculation, general_settings
from tseh.calculation.pricing import assess, effective_thresholds

pytestmark = pytest.mark.django_db


def token():
    return stamp(DomainState.objects.get(pk=1))


def item(kind="ingredient", loss="0", price="1000", size="1", unit="kg"):
    supplier = save_supplier(token(), {"name": "Тестовый поставщик", "note": ""})
    return create_ingredient(token(), {"name": "X", "kind": kind, "unit": unit, "loss": D(loss), "supplier_id": supplier,
        "pack_size": D(size), "pack_price": D(price), "price_date": date(2026, 11, 4)}, confirm_zero=D(price) == 0)


def line(key, quantity="1", unit="kg", prep=False):
    return {"preparation_id" if prep else "ingredient_id": key, "quantity": D(quantity), "unit": unit}


def prep(name, lines, output="1", unit="kg"):
    return save_preparation(token(), {"name": name, "output_quantity": D(output), "output_unit": unit}, lines)


def test_nested_recipes_and_output_recalculation(state):
    x = item(price="3000")
    a = prep("A", [line(x)], output="2")
    b = prep("B", [line(a, "0.5", prep=True)], output="0.5")
    dish = save_dish(token(), {"name": "Блюдо", "base_price": D(500)}, [line(b, "100", "g", True)])
    assert dish_calculation(Dish.objects.get(pk=dish))[0].total == D("150.00")
    save_preparation(token(), {"name": "A", "output_quantity": D("1.5"), "output_unit": "kg"}, [line(x)], a)
    assert dish_calculation(Dish.objects.get(pk=dish))[0].total == D("200.00")


def test_cycle_rolls_back_all_changes(state):
    x = item()
    a = prep("A", [line(x)])
    b = prep("B", [line(a, prep=True)])
    revision, logs = DomainState.objects.get(pk=1).revision, AuditEntry.objects.count()
    with pytest.raises(ValidationError, match="Цикл"):
        save_preparation(token(), {"name": "A changed", "output_quantity": D(1), "output_unit": "kg"}, [line(b, prep=True)], a)
    assert Preparation.objects.get(pk=a).name == "A"
    assert Preparation.objects.get(pk=a).lines.get().ingredient_id == x
    assert DomainState.objects.get(pk=1).revision == revision
    assert AuditEntry.objects.count() == logs


def test_packaging_cannot_have_losses(state):
    with pytest.raises(ValidationError):
        item("packaging", loss="1", unit="pc")
    assert not Ingredient.objects.exists()


def test_incompatible_used_output_unit_rejected(state):
    x = item()
    a = prep("A", [line(x)])
    save_dish(token(), {"name": "Блюдо", "base_price": D(1)}, [line(a, prep=True)])
    with pytest.raises(ValidationError):
        save_preparation(token(), {"name": "A", "output_quantity": D(1), "output_unit": "l"}, [line(x)], a)
    assert Preparation.objects.get(pk=a).output_unit == "kg"


def test_compatible_output_units_preserve_dependencies(state):
    x = item()
    a = prep("A", [line(x)], "2")
    dish = save_dish(token(), {"name": "Блюдо", "base_price": D(1)}, [line(a, "100", "g", True)])
    save_preparation(token(), {"name": "A", "output_quantity": D(2000), "output_unit": "g"}, [line(x)], a)
    assert dish_calculation(Dish.objects.get(pk=dish))[0].total == D("50.00")


def test_inherited_conflict_allowed_direct_conflict_rejected_and_recovery(state):
    x = item()
    save_thresholds(token(), {"green": D(40), "orange": D(60)})
    data = {"name": "Блюдо", "base_price": D(1000), "green_override": D(55), "orange_override": None}
    dish_id = save_dish(token(), data, [line(x, "0.4")])
    save_thresholds(token(), {"green": D(40), "orange": D(50)})
    dish = Dish.objects.get(pk=dish_id)
    g, o = effective_thresholds(dish, general_settings())
    assert (g, o) == (55, 50)
    result = assess(D(400), dish.base_price, D(0), g, o)
    assert result.zone == "invalid" and result.ratio == 40 and result.minimum is None
    # Unrelated recipe/name edits may preserve an existing inherited conflict.
    save_dish(token(), {**data, "name": "Переименовано"}, [line(x, "0.5")], dish_id)
    with pytest.raises(ValidationError):
        save_dish(token(), {**data, "green_override": D(56)}, [line(x)], dish_id)
    save_dish(token(), {**data, "green_override": None}, [line(x, "0.4")], dish_id)
    dish.refresh_from_db()
    assert effective_thresholds(dish, general_settings()) == (40, 50)


def test_global_direct_invalid_pair_rejected(state):
    with pytest.raises(ValidationError):
        save_thresholds(token(), {"green": D(60), "orange": D(40)})
    assert general_settings().green is None


def test_default_thresholds_empty_and_independent_overrides(state):
    assert general_settings().green is general_settings().orange is None
    x = item()
    save_thresholds(token(), {"green": D(40), "orange": D(60)})
    dish_id = save_dish(token(), {"name": "Блюдо", "base_price": D(1000), "green_override": None, "orange_override": D(120)}, [line(x)])
    save_thresholds(token(), {"green": D(45), "orange": D(65)})
    assert effective_thresholds(Dish.objects.get(pk=dish_id), general_settings()) == (45, 120)


def test_preparation_audit_failure_is_atomic(state):
    x = item()
    with patch("tseh.services.commands.audit", side_effect=RuntimeError("audit failed")):
        with pytest.raises(RuntimeError):
            prep("A", [line(x)])
    assert not Preparation.objects.exists()


def test_threshold_change_makes_preparation_form_stale(state):
    x = item()
    old = token()
    save_thresholds(token(), {"green": D(40), "orange": D(60)})
    with pytest.raises(StaleWrite):
        save_preparation(old, {"name": "A", "output_quantity": D(1), "output_unit": "kg"}, [line(x)])
