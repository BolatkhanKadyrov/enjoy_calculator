from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal as D
from threading import Barrier
from unittest.mock import patch
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connections, transaction

from tseh.models import AuditEntry, Dish, DomainState, Ingredient, Operation, Price, PriceApplication, Supplier
from tseh.services.commands import create_ingredient, save_dish, save_supplier, stamp, StaleWrite, update_ingredient
from tseh.services.queries import dish_calculation


def fresh():
    return stamp(DomainState.objects.get(pk=1))


def ingredient_data(supplier_id, **extra):
    return dict(name="Курица", unit="kg", loss=D("25"), pack_size=D(5), pack_price=D(10000),
                price_date=date(2026, 11, 4), supplier_id=supplier_id, **extra)


def setup_item():
    supplier = save_supplier(fresh(), {"name": "Поставщик", "note": ""})
    return create_ingredient(fresh(), ingredient_data(supplier))


@pytest.mark.django_db
def test_first_price_and_application_saved_atomically(state):
    item_id = setup_item()
    item = Ingredient.objects.get(pk=item_id)
    assert item.current_price.ingredient_id == item.pk
    assert PriceApplication.objects.count() == 1
    assert AuditEntry.objects.count() == 4
    assert AuditEntry.objects.filter(group=AuditEntry.objects.first().group).count() == 3


@pytest.mark.django_db
def test_first_zero_price_requires_explicit_service_confirmation_and_replay_binds_flag(state):
    data = ingredient_data(None)
    data["pack_price"] = D(0)
    token = fresh()
    before = (state.revision, AuditEntry.objects.count(), Operation.objects.count())
    for confirmation in [False, "on", 1, None]:
        with pytest.raises(ValidationError):
            create_ingredient(token, data, confirm_zero=confirmation)
        assert not Ingredient.objects.exists() and not Price.objects.exists() and not PriceApplication.objects.exists()
        state.refresh_from_db()
        assert (state.revision, AuditEntry.objects.count(), Operation.objects.count()) == before
    key = create_ingredient(token, data, confirm_zero=True)
    assert Ingredient.objects.get(pk=key).current_price.pack_price == D(0)
    assert PriceApplication.objects.count() == 1
    after = (DomainState.objects.get(pk=1).revision, AuditEntry.objects.count(), Operation.objects.count())
    assert create_ingredient(token, data, confirm_zero=True) == key
    with pytest.raises(StaleWrite):
        create_ingredient(token, data, confirm_zero=False)
    assert (DomainState.objects.get(pk=1).revision, AuditEntry.objects.count(), Operation.objects.count()) == after


@pytest.mark.django_db
def test_journal_failure_rolls_back_everything(state):
    supplier = save_supplier(fresh(), {"name": "Поставщик", "note": ""})
    before = DomainState.objects.get(pk=1).revision
    with patch("tseh.services.commands.audit", side_effect=RuntimeError("audit failure")):
        with pytest.raises(RuntimeError):
            create_ingredient(fresh(), ingredient_data(supplier))
    assert Ingredient.objects.count() == Price.objects.count() == PriceApplication.objects.count() == 0
    assert DomainState.objects.get(pk=1).revision == before
    assert Operation.objects.count() == 1


@pytest.mark.django_db
def test_duplicate_confirmation_is_idempotent(state):
    token = fresh()
    data = {"name": "Поставщик", "note": ""}
    first = save_supplier(token, data)
    second = save_supplier(token, data)
    assert first == second
    assert Supplier.objects.count() == AuditEntry.objects.count() == Operation.objects.count() == 1
    with pytest.raises(StaleWrite):
        save_supplier(token, {**data, "name": "Подменённое значение"})


@pytest.mark.django_db
def test_stale_form_does_not_overwrite(state):
    supplier = save_supplier(fresh(), {"name": "Первый", "note": ""})
    older = fresh()
    save_supplier(fresh(), {"name": "Изменённый", "note": ""}, supplier)
    with pytest.raises(StaleWrite):
        save_supplier(older, {"name": "Устаревший", "note": ""}, supplier)
    assert Supplier.objects.get(pk=supplier).name == "Изменённый"


@pytest.mark.django_db
def test_dependency_change_invalidates_dish(state):
    item_id = setup_item()
    old_token = fresh()
    update_ingredient(fresh(), item_id, {"name": "Курица", "loss": D(10)})
    with pytest.raises(StaleWrite):
        save_dish(old_token, {"name": "Блюдо", "base_price": D(500)}, [{"ingredient_id": item_id, "quantity": D(100), "unit": "g"}])
    assert not Dish.objects.exists()


@pytest.mark.django_db
def test_epoch_rejects_old_confirmation_even_if_revision_matches(state):
    old = fresh()
    state.epoch = uuid.uuid4()
    state.save()
    with pytest.raises(StaleWrite):
        save_supplier(old, {"name": "Старое подтверждение", "note": ""})


@pytest.mark.django_db
def test_quantity_survives_edit_and_cost_recomputes(state):
    item = setup_item()
    lines = [{"ingredient_id": item, "quantity": D("0.004000000001"), "unit": "kg"}]
    dish_id = save_dish(fresh(), {"name": "Блюдо", "base_price": D(100)}, lines)
    dish = Dish.objects.get(pk=dish_id)
    before = dish_calculation(dish)[0]
    save_dish(fresh(), {"name": "Блюдо", "base_price": D(100)}, lines, dish_id)
    assert dish.lines.get().quantity == D("0.004000000001")
    assert dish_calculation(dish)[0] == before
    update_ingredient(fresh(), item, {"name": "Курица", "loss": D(0)})
    assert dish_calculation(dish)[0].total == D("8.00")


@pytest.mark.django_db
def test_bad_recipe_rolls_back_existing_recipe(state):
    item = setup_item()
    old = [{"ingredient_id": item, "quantity": D(100), "unit": "g"}]
    dish_id = save_dish(fresh(), {"name": "Блюдо", "base_price": D(100)}, old)
    with pytest.raises(ValidationError):
        save_dish(fresh(), {"name": "Не должно сохраниться", "base_price": D(200)}, [{**old[0], "unit": "l"}], dish_id)
    dish = Dish.objects.get(pk=dish_id)
    assert dish.name == "Блюдо"
    assert dish.lines.get().unit == "g"


@pytest.mark.django_db(transaction=True)
def test_database_requires_matching_current_price(state):
    one, two = setup_item(), setup_item()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Ingredient.objects.filter(pk=one).update(current_price_id=Ingredient.objects.get(pk=two).current_price_id)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Ingredient.objects.create(name="Нет цены", unit="kg", current_price_id=uuid.uuid4())


@pytest.mark.django_db(transaction=True)
def test_concurrent_writers_only_one_wins(state):
    supplier = save_supplier(fresh(), {"name": "Исходное", "note": ""})
    tokens = [fresh(), fresh()]
    barrier = Barrier(2)
    def worker(index):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            save_supplier(tokens[index], {"name": f"Правка {index}", "note": ""}, supplier)
            return "saved"
        except StaleWrite:
            return "stale"
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, [0, 1]))
    assert sorted(results) == ["saved", "stale"]
    assert AuditEntry.objects.count() == 2
    assert Operation.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_concurrent_identical_confirmation_only_one_event(state):
    token = fresh()
    barrier = Barrier(2)
    def worker(_):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return save_supplier(token, {"name": "Один поставщик", "note": ""})
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, [0, 1]))
    assert results[0] == results[1]
    assert Supplier.objects.count() == AuditEntry.objects.count() == 1
