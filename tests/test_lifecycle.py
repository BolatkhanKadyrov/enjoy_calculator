from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal as D
from io import StringIO
from threading import Barrier
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connections, transaction

from tseh.models import AuditEntry, Dish, DishLine, DomainState, Ingredient, Preparation, Price, PriceApplication, Supplier
from tseh.services.commands import StaleWrite, save_dish, save_supplier
from tseh.services.lifecycle import catalog_dependencies, delete_catalog_object, purge_due_dishes, transition_catalog, transition_dish
from tests.test_stage2_services import item, line, prep, token

pytestmark = pytest.mark.django_db
T = datetime(2026, 10, 1, 12, tzinfo=dt_timezone.utc)


def dish(name="Проверяемое блюдо", ingredient=None):
    ingredient = ingredient or item()
    return save_dish(token(), {"name": name, "base_price": D(500)}, [line(ingredient)])


def trash(key, moment=T):
    transition_dish(token(), key, "archive", now=moment - timedelta(days=200))
    transition_dish(token(), key, "trash", now=moment)


def facts():
    return (DomainState.objects.get(pk=1).revision, AuditEntry.objects.count(),
            list(Dish.objects.order_by("id").values()), list(DishLine.objects.order_by("id").values()))


def test_transition_chain_preserves_recipe_and_historical_dates(state):
    key = dish()
    before_lines = list(DishLine.objects.filter(dish_id=key).values())
    transition_dish(token(), key, "archive", now=T)
    archived = Dish.objects.get(pk=key)
    assert archived.status == "archived" and archived.archived_at == T and archived.purge_at is None
    trash_time = T + timedelta(days=10, microseconds=123456)
    transition_dish(token(), key, "trash", now=trash_time)
    trashed = Dish.objects.get(pk=key)
    assert trashed.trashed_at == trash_time
    assert trashed.purge_at == datetime(2026, 11, 10, 12, 0, 0, 123456, tzinfo=dt_timezone.utc)
    restored_time = T + timedelta(days=12)
    transition_dish(token(), key, "restore", now=restored_time)
    restored = Dish.objects.get(pk=key)
    assert restored.status == "archived" and restored.archived_at == restored_time
    assert restored.trashed_at == trash_time and restored.purge_at is None
    transition_dish(token(), key, "activate", now=T + timedelta(days=13))
    active = Dish.objects.get(pk=key)
    assert active.status == "active" and active.archived_at == restored_time and active.trashed_at == trash_time
    assert list(DishLine.objects.filter(dish_id=key).values()) == before_lines
    assert list(AuditEntry.objects.filter(object_id=str(key)).order_by("id").values_list("action", flat=True)) == [
        "create", "archive", "trash", "restore", "activate"]


@pytest.mark.parametrize("source,action", [("active", "trash"), ("active", "restore"), ("active", "activate"),
    ("archived", "archive"), ("archived", "restore"), ("trashed", "activate"), ("trashed", "archive"), ("trashed", "trash")])
def test_invalid_transition_is_atomic(state, source, action):
    key = dish()
    if source != "active":
        transition_dish(token(), key, "archive", now=T)
    if source == "trashed":
        transition_dish(token(), key, "trash", now=T)
    before = facts()
    with pytest.raises(ValidationError):
        transition_dish(token(), key, action, now=T)
    assert facts() == before


def test_repeated_and_stale_confirmation_never_repeats_transition(state):
    key = dish()
    confirmation = token()
    stale = token()
    transition_dish(confirmation, key, "archive", now=T)
    before = facts()
    assert transition_dish(confirmation, key, "archive", now=T + timedelta(days=5)) == key
    assert facts() == before
    with pytest.raises(StaleWrite):
        transition_dish(stale, key, "activate", now=T)
    with pytest.raises(StaleWrite):
        transition_dish(confirmation, key, "trash", now=T)
    assert facts() == before


def test_transition_failure_rolls_back_state_dates_and_audit(state):
    key = dish()
    before = facts()
    with patch("tseh.services.lifecycle.audit", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            transition_dish(token(), key, "archive", now=T)
    assert facts() == before
    with pytest.raises(ValidationError):
        transition_dish(token(), key, "archive", now=datetime(2026, 10, 1))
    assert facts() == before


def test_purge_boundary_keeps_other_states_and_retains_journal_and_recipe_snapshot(state):
    key = dish("Истекает сейчас")
    later, active, archived = dish("Позже"), dish("Активно"), dish("В архиве")
    trash(key)
    trash(later, T + timedelta(microseconds=1))
    transition_dish(token(), archived, "archive", now=T - timedelta(days=300))
    existing_audit_ids = set(AuditEntry.objects.values_list("id", flat=True))
    revision = DomainState.objects.get(pk=1).revision
    moment = datetime(2026, 10, 31, 12, tzinfo=dt_timezone.utc)
    assert purge_due_dishes(now=moment - timedelta(microseconds=1)) == 0
    assert DomainState.objects.get(pk=1).revision == revision
    assert purge_due_dishes(now=moment) == 1
    assert set(Dish.objects.values_list("id", flat=True)) == {later, active, archived}
    assert not DishLine.objects.filter(dish_id=key).exists()
    assert existing_audit_ids.issubset(set(AuditEntry.objects.values_list("id", flat=True)))
    entry = AuditEntry.objects.get(action="purge", object_id=str(key))
    assert entry.source == "cleanup" and entry.object_name == "Истекает сейчас"
    assert entry.before["status"] == "trashed" and entry.after is None
    assert len(entry.before["lines"]) == 1 and D(entry.before["lines"][0]["quantity"]) == D(1)
    assert DomainState.objects.get(pk=1).revision == revision + 1
    before = facts()
    assert purge_due_dishes(now=moment) == 0 and facts() == before


def test_restored_deadline_not_historical_trash_date_controls_purge(state):
    key = dish()
    trash(key)
    # JSON restoration uses the saved 12-day duration and keeps the original date.
    Dish.objects.filter(pk=key).update(purge_at=datetime(2026, 12, 1, 12, tzinfo=dt_timezone.utc))
    assert purge_due_dishes(now=datetime(2026, 11, 19, 12, tzinfo=dt_timezone.utc)) == 0
    assert Dish.objects.get(pk=key).trashed_at == T
    assert purge_due_dishes(now=datetime(2026, 12, 1, 12, tzinfo=dt_timezone.utc)) == 1


def test_purge_batch_failure_rolls_back_every_deleted_dish(state):
    a, b = dish("A"), dish("B")
    trash(a)
    trash(b)
    before = facts()
    from tseh.services.lifecycle import audit
    calls = 0
    def failing_audit(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second audit failed")
        return audit(*args, **kwargs)
    with patch("tseh.services.lifecycle.audit", side_effect=failing_audit):
        with pytest.raises(RuntimeError):
            purge_due_dishes(now=T + timedelta(days=30))
    assert facts() == before


def test_management_command_uses_current_clock_and_is_idempotent(state):
    key = dish()
    trash(key)
    out = StringIO()
    with patch("tseh.services.lifecycle.timezone.now", return_value=T + timedelta(days=31)):
        call_command("purge_trash", stdout=out)
        call_command("purge_trash", stdout=out)
    assert "1." in out.getvalue() and "0." in out.getvalue()
    assert AuditEntry.objects.filter(action="purge").count() == 1


@pytest.mark.parametrize("values", [{"status": "other"}, {"status": "archived"},
    {"status": "trashed", "archived_at": T, "trashed_at": T}, {"purge_at": T}, {"trashed_at": T}])
def test_database_rejects_inconsistent_lifecycle_dates(state, values):
    key = dish()
    with pytest.raises(IntegrityError), transaction.atomic():
        Dish.objects.filter(pk=key).update(**values)


def test_catalog_deletion_lists_archive_trash_price_and_nested_dependencies(state):
    raw = item()
    a = prep("Тесто", [line(raw)])
    b = prep("Смесь", [line(a, prep=True)])
    key = save_dish(token(), {"name": "В корзине", "base_price": D(500)}, [line(a, prep=True)])
    trash(key)
    dependencies = catalog_dependencies("preparation", a)
    assert "Заготовка: Смесь" in dependencies
    assert "Блюдо: В корзине (В корзине)" in dependencies
    before = facts()
    with pytest.raises(ValidationError):
        transition_catalog(token(), "preparation", a, "trash", now=T)
    assert facts() == before and Preparation.objects.filter(pk=a).exists()
    assert "Заготовка: Тесто" in catalog_dependencies("ingredient", raw)
    with pytest.raises(ValidationError):
        transition_catalog(token(), "ingredient", raw, "trash", now=T)
    supplier = Ingredient.objects.get(pk=raw).current_price.supplier
    assert any("Цены позиции" in text for text in catalog_dependencies("supplier", supplier.pk))
    with pytest.raises(ValidationError):
        transition_catalog(token(), "supplier", supplier.pk, "trash", now=T)
    assert Preparation.objects.filter(pk=b).exists()


def test_unused_supplier_and_preparation_delete_with_full_audit_and_replay(state):
    raw = item()
    key = prep("Свободная заготовка", [line(raw)])
    assert catalog_dependencies("preparation", key) == []
    with pytest.raises(ValidationError):
        delete_catalog_object(token(), "preparation", key)
    transition_catalog(token(), "preparation", key, "trash", now=T)
    confirmation = token()
    assert delete_catalog_object(confirmation, "preparation", key) == key
    assert not Preparation.objects.filter(pk=key).exists() and Ingredient.objects.filter(pk=raw).exists()
    entry = AuditEntry.objects.get(object_id=str(key), action="purge")
    assert entry.before["name"] == "Свободная заготовка" and len(entry.before["lines"]) == 1
    before = facts()
    assert delete_catalog_object(confirmation, "preparation", key) == key and facts() == before
    supplier = save_supplier(token(), {"name": "Не используется", "note": "Контакт"})
    assert catalog_dependencies("supplier", supplier) == []
    transition_catalog(token(), "supplier", supplier, "trash", now=T)
    delete_catalog_object(token(), "supplier", supplier)
    assert not Supplier.objects.filter(pk=supplier).exists()


@pytest.mark.parametrize("kind", ["supplier", "ingredient", "preparation"])
def test_catalog_trash_restore_is_indefinite_and_keeps_history(state, kind):
    raw = item()
    keys = {"supplier": save_supplier(token(), {"name": "Свободный", "note": ""}),
            "preparation": prep("Неиспользуемая", [line(raw)]), "ingredient": item()}
    models = {"supplier": Supplier, "ingredient": Ingredient, "preparation": Preparation}
    key, model = keys[kind], models[kind]
    saved_price_ids = set(Price.objects.values_list("id", flat=True))
    transition_catalog(token(), kind, key, "trash", now=T)
    trashed = model.objects.get(pk=key)
    assert trashed.status == "trashed" and trashed.trashed_at == T
    assert not hasattr(trashed, "purge_at")
    assert purge_due_dishes(now=T + timedelta(days=10000)) == 0
    assert model.objects.filter(pk=key).exists()
    transition_catalog(token(), kind, key, "restore", now=T + timedelta(days=300))
    restored = model.objects.get(pk=key)
    assert restored.status == "active" and restored.trashed_at == T
    assert set(Price.objects.values_list("id", flat=True)) == saved_price_ids
    assert list(AuditEntry.objects.filter(object_id=str(key)).order_by("id").values_list("action", flat=True))[-2:] == ["trash", "restore"]


def test_unused_ingredient_explicit_purge_removes_only_own_prices_events_and_preserves_full_audit(state):
    from tseh.services.prices import apply_price, create_price
    key, other = item(), item()
    original = Ingredient.objects.get(pk=key).current_price_id
    create_price(token(), key, {"supplier_id": None, "price_date": "2026-11-05", "pack_size": D(2),
        "pack_price": D(5000), "comment": "Цена Б", "is_one_off": True}, apply=True)
    apply_price(token(), key, original)
    own_prices = set(Price.objects.filter(ingredient_id=key).values_list("id", flat=True))
    own_events = list(PriceApplication.objects.filter(price__ingredient_id=key).order_by("id").values_list("price_id", flat=True))
    before_audits = set(AuditEntry.objects.values_list("id", flat=True))
    assert catalog_dependencies("ingredient", key) == []
    transition_catalog(token(), "ingredient", key, "trash", now=T)
    confirmation = token()
    assert transition_catalog(confirmation, "ingredient", key, "purge") == key
    assert not Ingredient.objects.filter(pk=key).exists()
    assert not Price.objects.filter(pk__in=own_prices).exists()
    assert not PriceApplication.objects.filter(price_id__in=own_prices).exists()
    assert Ingredient.objects.filter(pk=other).exists() and Price.objects.filter(ingredient_id=other).count() == 1
    assert before_audits.issubset(set(AuditEntry.objects.values_list("id", flat=True)))
    entry = AuditEntry.objects.get(action="purge", object_id=str(key))
    assert entry.source == "manual" and entry.after is None and entry.before["status"] == "trashed"
    assert {row["id"] for row in entry.before["prices"]} == {str(pk) for pk in own_prices}
    assert [row["price_id"] for row in entry.before["applications"]] == [str(pk) for pk in own_events]
    assert entry.before["current_price_id"] == str(original)
    after = facts()
    assert transition_catalog(confirmation, "ingredient", key, "purge") == key and facts() == after


def test_catalog_trash_rejects_purchase_dependency_and_purge_rechecks_new_recipe(state):
    from tests.test_purchase_web import line_input, purchase
    key = item()
    purchase(line_input(key))
    assert any("Закупка от" in text for text in catalog_dependencies("ingredient", key))
    with pytest.raises(ValidationError):
        transition_catalog(token(), "ingredient", key, "trash", now=T)
    free = item()
    transition_catalog(token(), "ingredient", free, "trash", now=T)
    # Simulate a malformed legacy/external reference. Final deletion still rechecks,
    # even though the ordinary service forbids adding trashed components.
    obj = Dish.objects.create(name="Внешняя ссылка", base_price=D(1))
    DishLine.objects.create(dish=obj, ingredient_id=free, quantity=D(1), unit="kg", position=0)
    before = facts()
    with pytest.raises(ValidationError):
        transition_catalog(token(), "ingredient", free, "purge")
    assert facts() == before and Ingredient.objects.filter(pk=free).exists()


def test_catalog_purge_failure_rolls_back_price_history_and_audit(state):
    key = item()
    transition_catalog(token(), "ingredient", key, "trash", now=T)
    before = facts(), list(Price.objects.values()), list(PriceApplication.objects.values())
    from tseh.services.lifecycle import _purge_unused_ingredient
    def fail_after_delete(obj):
        _purge_unused_ingredient(obj)
        raise RuntimeError("after deletion")
    with patch("tseh.services.lifecycle._purge_unused_ingredient", side_effect=fail_after_delete):
        with pytest.raises(RuntimeError):
            transition_catalog(token(), "ingredient", key, "purge")
    assert (facts(), list(Price.objects.values()), list(PriceApplication.objects.values())) == before


def test_catalog_stale_and_repeated_requests_cannot_restore_newer_state(state):
    key = item()
    confirmation, stale = token(), token()
    transition_catalog(confirmation, "ingredient", key, "trash", now=T)
    transition_catalog(token(), "ingredient", key, "restore", now=T)
    before = facts()
    assert transition_catalog(confirmation, "ingredient", key, "trash", now=T) == key
    assert Ingredient.objects.get(pk=key).status == "active" and facts() == before
    with pytest.raises(StaleWrite):
        transition_catalog(stale, "ingredient", key, "trash", now=T)
    assert facts() == before


@pytest.mark.parametrize("model", [Supplier, Ingredient, Preparation])
def test_catalog_database_requires_valid_status_and_trash_date(state, model):
    raw = item()
    obj = {Supplier: Supplier.objects.first(), Ingredient: Ingredient.objects.get(pk=raw),
           Preparation: Preparation.objects.get(pk=prep("Смесь", [line(raw)]))}[model]
    for values in ({"status": "archived"}, {"status": "trashed", "trashed_at": None}):
        with pytest.raises(IntegrityError), transaction.atomic():
            model.objects.filter(pk=obj.pk).update(**values)


@pytest.mark.django_db(transaction=True)
def test_two_cleanup_workers_delete_once_and_leave_one_audit_group(state):
    key = dish()
    trash(key)
    gate = Barrier(2)
    def clean():
        connections.close_all()
        try:
            gate.wait(timeout=10)
            return purge_due_dishes(now=T + timedelta(days=30))
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: clean(), range(2)))
    assert sorted(results) == [0, 1]
    assert not Dish.objects.filter(pk=key).exists()
    assert AuditEntry.objects.filter(action="purge", object_id=str(key)).count() == 1
