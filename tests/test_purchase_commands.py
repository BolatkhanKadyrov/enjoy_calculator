from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from decimal import Decimal as D
from fractions import Fraction as F
from threading import Barrier
from unittest.mock import patch
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections

from tseh.models import AuditEntry, DomainState, Ingredient, Operation, Price, PriceApplication, Purchase, PurchaseLine, UnallocatedDelivery
from tseh.services.commands import StaleWrite, save_dish, save_preparation
from tseh.services.price_queries import price_history
from tseh.services.prices import apply_price, correct_price, delete_price
from tseh.services.purchases import (attach_delivery, delete_delivery, delete_purchase_line,
    prepare_attach_delivery, prepare_delete_purchase_line, prepare_purchase, save_delivery, save_purchase)
from tseh.services.purchase_queries import purchase_impact
from tseh.services.queries import dish_calculation
from tests.test_stage2_services import item, token

pytestmark = pytest.mark.django_db


def data(delivery="3000", **extra):
    return {"purchase_date": date(2026, 11, 4), "delivery_total": D(delivery), "comment": "Закупка", **extra}


def row(key, size="5", price="5000", count="4", **extra):
    return {"ingredient_id": key, "pack_size": D(size), "pack_price": D(price),
            "pack_count": D(count) if count is not None else None, **extra}


def source_rows(purchase):
    return [{"id": line.id, **{key: getattr(line, key) for key in
            ("ingredient_id", "pack_size", "pack_price", "pack_count", "comment", "is_one_off")}}
            for line in purchase.lines.all()]


def setup_purchase(delivery="3000", apply=False):
    a, b = item(), item()
    key = save_purchase(token(), data(delivery), [row(a), row(b, "10", "5000", "2")],
                        apply_ingredient_ids=[a, b] if apply else [])
    return Purchase.objects.get(pk=key), a, b


def facts():
    return (DomainState.objects.get(pk=1).revision, AuditEntry.objects.count(), Operation.objects.count(),
            Purchase.objects.count(), PurchaseLine.objects.count(), Price.objects.count(),
            PriceApplication.objects.count(), UnallocatedDelivery.objects.count())


def test_create_exact_offer_no_implicit_application_and_preview_no_mutation(state):
    a, b = item(), item()
    payload, lines = data(), [row(a), row(b, "10", "5000", "2")]
    original = deepcopy((payload, lines))
    before = facts()
    plan = prepare_purchase(payload, lines)
    impact = purchase_impact(plan)
    assert facts() == before and (payload, lines) == original
    assert impact["dishes"] == []
    assert [line.delivery_share for line in plan.lines] == [D(2000), D(1000)]
    assert [plan.prices[key].effective_unit_price for key in [a, b]] == [F(1100), F(550)]
    stamp = token()
    key = save_purchase(stamp, payload, lines)
    assert Ingredient.objects.get(pk=a).current_price.purchase_id is None
    assert PriceApplication.objects.count() == before[6]
    after = facts()
    assert save_purchase(stamp, payload, lines) == key and facts() == after
    with pytest.raises(StaleWrite):
        save_purchase(stamp, data("3001"), lines)
    assert facts() == after


def test_correction_changes_entire_purchase_history_and_nested_current_calculations(state):
    purchase, a, b = setup_purchase(apply=True)
    prep = save_preparation(token(), {"name": "Смесь", "output_quantity": D(2), "output_unit": "kg"},
        [{"ingredient_id": a, "quantity": D(1), "unit": "kg"}, {"ingredient_id": b, "quantity": D(1), "unit": "kg"}])
    dish = save_dish(token(), {"name": "Блюдо", "base_price": D(500)}, [{"preparation_id": prep, "quantity": D(".1"), "unit": "kg"}])
    from tseh.models import Dish
    assert dish_calculation(Dish.objects.get(pk=dish))[0].total == D("82.50")
    events = list(PriceApplication.objects.values_list("id", "applied_at", "price_id"))
    price_ids = dict(purchase.prices.values_list("ingredient_id", "id"))
    plan = prepare_purchase(data("6000"), source_rows(purchase), purchase_id=purchase.id, reason="Уточнение счёта")
    impact = purchase_impact(plan)
    assert impact["dishes"][0]["after"].total == D("90.00")
    save_purchase(token(), data("6000"), source_rows(purchase), purchase_id=purchase.id, reason="Уточнение счёта")
    assert dict(purchase.prices.values_list("ingredient_id", "id")) == price_ids
    assert list(PriceApplication.objects.values_list("id", "applied_at", "price_id")) == events
    assert dish_calculation(Dish.objects.get(pk=dish))[0].total == D("90.00")
    assert price_history(Ingredient.objects.get(pk=a))[-1].unit_price == 1200
    assert price_history(Ingredient.objects.get(pk=b))[-1].unit_price == 600
    logs = AuditEntry.objects.filter(object_type="Price", action="update")
    assert logs.count() == 2 and all(log.before["applications"] for log in logs)


@pytest.mark.parametrize("action,delivery,price", [("remove_all", "0", "1000"), ("drop_share", "2000", "1100"), ("reallocate", "3000", "1150")])
def test_three_deletion_options_and_current_replacement(state, action, delivery, price):
    purchase, a, b = setup_purchase(apply=True)
    line = purchase.lines.get(ingredient_id=b)
    replacement = Price.objects.get(ingredient_id=b, purchase__isnull=True)
    before = facts()
    with pytest.raises(ValidationError, match="замен"):
        delete_purchase_line(token(), purchase.id, line.id, delivery_action=action)
    assert facts() == before
    stamp = token()
    plan = prepare_delete_purchase_line(purchase.id, line.id, delivery_action=action, replacement_price_id=replacement.id)
    assert plan.before["delivery_total"] == "3000.00" and plan.deleted_line.delivery_share == 1000
    assert plan.remaining_delivery == D(delivery)
    assert delete_purchase_line(stamp, purchase.id, line.id, delivery_action=action, replacement_price_id=replacement.id) == purchase.id
    purchase.refresh_from_db()
    assert purchase.delivery_total == D(delivery)
    assert purchase.prices.get(ingredient_id=a).effective_unit_price == F(price)
    assert Ingredient.objects.get(pk=b).current_price_id == replacement.id
    assert not PriceApplication.objects.filter(price__purchase=purchase, price__ingredient_id=b).exists()
    after = facts()
    assert delete_purchase_line(stamp, purchase.id, line.id, delivery_action=action, replacement_price_id=replacement.id) == purchase.id
    assert facts() == after


def test_drop_share_preserves_exact_remaining_cents_and_positions(state):
    keys = [item() for _ in range(3)]
    purchase = Purchase.objects.get(pk=save_purchase(token(), data("1"), [row(key, "1", "1", "1") for key in keys]))
    first = purchase.lines.first()
    remaining = list(purchase.lines.exclude(pk=first.id).values_list("id", "position", "delivery_share"))
    delete_purchase_line(token(), purchase.id, first.id, delivery_action="drop_share")
    purchase.refresh_from_db()
    assert purchase.delivery_total == D(".66")
    assert list(purchase.lines.values_list("id", "position", "delivery_share")) == remaining


def preserved_share_purchase():
    """Independent counterexample: the retained shares differ from fresh quotas."""
    keys = [item() for _ in range(7)]
    costs = ["890", "180", "81", "81", "81", "81", "6"]
    purchase = Purchase.objects.get(pk=save_purchase(token(), data(".14"),
        [row(key, "1", cost, "1") for key, cost in zip(keys, costs)], apply_ingredient_ids=keys[:2]))
    assert list(purchase.lines.values_list("delivery_share", flat=True)) == [D(".09"), *([D(".01")] * 5), D(0)]
    for line in list(purchase.lines.all())[2:]:
        delete_purchase_line(token(), purchase.pk, line.pk, delivery_action="drop_share")
    purchase.refresh_from_db()
    assert purchase.delivery_total == D(".10")
    assert list(purchase.lines.values_list("delivery_share", flat=True)) == [D(".09"), D(".01")]
    return purchase, keys


def test_noop_and_metadata_corrections_preserve_previously_saved_shares(state):
    purchase, keys = preserved_share_purchase()
    events = list(PriceApplication.objects.values_list("id", "price_id", "applied_at"))
    original_rows = list(purchase.lines.values_list("id", "position", "delivery_share"))
    values, lines = data(".10"), source_rows(purchase)
    changes = [{}, {"comment": "Уточнение комментария"}, {"purchase_date": date(2026, 11, 5)},
               {"supplier_id": Ingredient.objects.get(pk=keys[0]).prices.get(purchase__isnull=True).supplier_id}]
    for change in changes:
        values.update(change)
        plan = prepare_purchase(values, lines, purchase_id=purchase.pk)
        assert [line.delivery_share for line in plan.lines] == [D(".09"), D(".01")]
        save_purchase(token(), values, lines, purchase_id=purchase.pk)
    lines[0].update(comment="Заметка к строке", is_one_off=True)
    save_purchase(token(), values, lines, purchase_id=purchase.pk)
    assert list(purchase.lines.values_list("id", "position", "delivery_share")) == original_rows
    assert list(PriceApplication.objects.values_list("id", "price_id", "applied_at")) == events
    assert Ingredient.objects.get(pk=keys[0]).current_price.effective_unit_price == F("890.09")
    assert Ingredient.objects.get(pk=keys[1]).current_price.effective_unit_price == F("180.01")
    assert purchase.lines.get(pk=lines[0]["id"]).comment == "Заметка к строке"


@pytest.mark.parametrize("change,expected", [("pack_price", [".08", ".02"]),
    ("pack_size", [".08", ".02"]), ("pack_count", [".07", ".03"]),
    ("delivery_total", [".17", ".03"]), ("composition", [".08", ".02", "0"])])
def test_financial_correction_reallocates_all_previously_saved_shares(state, change, expected):
    purchase, keys = preserved_share_purchase()
    values, lines = data(".10"), source_rows(purchase)
    if change == "pack_price":
        lines[0]["pack_price"] = D(900)
    elif change == "pack_size":
        lines[0]["pack_size"] = D(2)
    elif change == "pack_count":
        lines[1]["pack_count"] = D(2)
    elif change == "delivery_total":
        values["delivery_total"] = D(".20")
    else:
        lines.append(row(keys[2], "1", "1", "1"))
    events = list(PriceApplication.objects.values_list("id", "price_id", "applied_at"))
    plan = prepare_purchase(values, lines, purchase_id=purchase.pk, reason="Исправление расчётных данных")
    assert [line.delivery_share for line in plan.lines] == list(map(D, expected))
    save_purchase(token(), values, lines, purchase_id=purchase.pk, reason="Исправление расчётных данных")
    assert list(purchase.lines.values_list("delivery_share", flat=True)) == list(map(D, expected))
    assert list(PriceApplication.objects.values_list("id", "price_id", "applied_at")) == events


def test_zero_delivery_transfer_preserves_previously_saved_shares(state):
    purchase, keys = preserved_share_purchase()
    delivery = save_delivery(token(), {"amount": D(0), "note": "Нулевой расход"})
    original = list(purchase.lines.values_list("id", "position", "delivery_share"))
    events = list(PriceApplication.objects.values_list("id", "price_id", "applied_at"))
    plan = prepare_attach_delivery(delivery, purchase.pk, confirm_addition=True)
    assert [line.delivery_share for line in plan.lines] == [D(".09"), D(".01")]
    stamp = token()
    assert attach_delivery(stamp, delivery, purchase.pk, confirm_addition=True) == purchase.pk
    assert list(purchase.lines.values_list("id", "position", "delivery_share")) == original
    assert list(PriceApplication.objects.values_list("id", "price_id", "applied_at")) == events
    assert not UnallocatedDelivery.objects.filter(pk=delivery).exists()
    assert Ingredient.objects.get(pk=keys[0]).current_price.effective_unit_price == F("890.09")
    after = facts()
    assert attach_delivery(stamp, delivery, purchase.pk, confirm_addition=True) == purchase.pk
    assert facts() == after


def test_duplicate_lines_are_weighted_and_removal_keeps_aggregate_price_and_events(state):
    a, b = item(), item()
    key = save_purchase(token(), data("1000"), [row(a, "1", "1000", "2"), row(a, "2", "3000", "4"), row(b, "2", "2000", "3")], apply_ingredient_ids=[a])
    purchase = Purchase.objects.get(pk=key)
    price = purchase.prices.get(ingredient_id=a)
    assert price.pack_size is None and price.pack_price is None
    assert price.effective_unit_price == 1470
    events = list(PriceApplication.objects.values_list("id", "price_id", "applied_at"))
    first = purchase.lines.first()
    delete_purchase_line(token(), key, first.id, delivery_action="drop_share")
    price.refresh_from_db()
    assert price.effective_unit_price == 1575 and price.pack_size == 2
    assert list(PriceApplication.objects.values_list("id", "price_id", "applied_at")) == events
    assert Ingredient.objects.get(pk=a).current_price_id == price.id


@pytest.mark.parametrize("action,amount,delivery_count", [("keep", "3000", 1), ("delete", "3000", 0), ("keep", "0", 1), ("delete", "0", 0)])
def test_last_line_removes_purchase_and_replay_keeps_delivery_once(state, action, amount, delivery_count):
    a = item()
    purchase = Purchase.objects.get(pk=save_purchase(token(), data(amount), [row(a)]))
    line = purchase.lines.get()
    stamp = token()
    key = purchase.id
    assert delete_purchase_line(stamp, key, line.id, delivery_action=action) == key
    assert not Purchase.objects.filter(pk=key).exists()
    assert UnallocatedDelivery.objects.count() == delivery_count
    if delivery_count:
        assert UnallocatedDelivery.objects.get().amount == D(amount)
    before = facts()
    assert delete_purchase_line(stamp, key, line.id, delivery_action=action) == key and facts() == before
    assert AuditEntry.objects.filter(object_type="Purchase", object_id=str(key), action="delete").exists()


@pytest.mark.parametrize("original,expected,shares", [("0", "3000", [2000, 1000]), ("600", "3600", [2400, 1200])])
def test_transfer_whole_delivery_exact_addition_and_no_price_application(state, original, expected, shares):
    purchase, a, b = setup_purchase(original)
    delivery = save_delivery(token(), {"amount": D(3000), "note": "Перевозчик"})
    before = facts()
    if original != "0":
        with pytest.raises(ValidationError, match="Подтвердите"):
            attach_delivery(token(), delivery, purchase.id)
        assert facts() == before
    stamp = token()
    attach_delivery(stamp, delivery, purchase.id, confirm_addition=True)
    purchase.refresh_from_db()
    assert purchase.delivery_total == D(expected)
    assert list(purchase.lines.values_list("delivery_share", flat=True)) == list(map(D, shares))
    assert not UnallocatedDelivery.objects.filter(pk=delivery).exists()
    assert PriceApplication.objects.count() == before[6]
    log = AuditEntry.objects.get(object_type="UnallocatedDelivery", object_id=str(delivery), action="transfer")
    assert D(log.before["amount"]) == 3000 and D(log.after["delivery_total"]) == D(expected)
    after = facts()
    assert attach_delivery(stamp, delivery, purchase.id, confirm_addition=True) == purchase.id and facts() == after


def test_delivery_correction_requires_reason_and_delete_replays(state):
    key = save_delivery(token(), {"amount": D(3000)})
    before = facts()
    with pytest.raises(ValidationError, match="Причина"):
        save_delivery(token(), {"amount": D(2900)}, key)
    assert facts() == before
    save_delivery(token(), {"amount": D(2900)}, key, reason="Ошибка ввода")
    assert UnallocatedDelivery.objects.get(pk=key).amount == 2900
    stamp = token()
    delete_delivery(stamp, key)
    after = facts()
    assert delete_delivery(stamp, key) == key and facts() == after


def test_missing_quantities_preserved_without_delivery_then_explicitly_supplied(state):
    a, b = item(), item()
    purchase = Purchase.objects.get(pk=save_purchase(token(), data("0"), [row(a, count=None), row(b, count=None)]))
    assert list(purchase.lines.values_list("pack_count", flat=True)) == [None, None]
    delivery = save_delivery(token(), {"amount": D(3000)})
    before = facts()
    with pytest.raises(ValidationError, match="количество"):
        attach_delivery(token(), delivery, purchase.id)
    assert facts() == before
    quantities = {str(line.id): "2" for line in purchase.lines.all()}
    attach_delivery(token(), delivery, purchase.id, quantities=quantities)
    assert list(purchase.lines.values_list("pack_count", flat=True)) == [D(2), D(2)]


@pytest.mark.parametrize("field,value", [("pack_count", D(0)), ("pack_size", D(-1)), ("pack_price", 2.5), ("pack_price", D("1.001")), ("pack_count", D("NaN")), ("ingredient_id", uuid.uuid4()), ("is_one_off", "false")])
def test_service_rejects_invalid_inputs_without_partial_state(state, field, value):
    a = item()
    line = row(a)
    line[field] = value
    before = facts()
    with pytest.raises(ValidationError):
        save_purchase(token(), data(), [line])
    assert facts() == before


def test_zero_duplicate_weights_and_correction_paths_enforced_by_service(state):
    a = item()
    before = facts()
    for lines, kwargs in [([row(a, price="0")], {}), ([row(a, count=None), row(a, count="2")], {}),
                          ([row(a, price="0")], {"confirm_zero": True})]:
        with pytest.raises(ValidationError):
            save_purchase(token(), data(), lines, **kwargs)
        assert facts() == before
    purchase = Purchase.objects.get(pk=save_purchase(token(), data("0"), [row(a)]))
    price = purchase.prices.get()
    for call in [lambda: correct_price(token(), a, price.id, {"pack_price": D(1)}),
                 lambda: delete_price(token(), a, price.id),
                 lambda: save_purchase(token(), data("1"), source_rows(purchase), purchase_id=purchase.id),
                 lambda: save_purchase(token(), data("0"), [], purchase_id=purchase.id)]:
        before = facts()
        with pytest.raises(ValidationError):
            call()
        assert facts() == before


def test_audit_failure_rolls_back_replacement_delete_delivery_and_price_updates(state):
    purchase, a, b = setup_purchase(apply=True)
    removed = purchase.lines.get(ingredient_id=b)
    replacement = Price.objects.get(ingredient_id=b, purchase__isnull=True)
    before = facts()
    original = list(purchase.lines.values_list("id", "delivery_share"))
    with patch("tseh.services.purchases.audit", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            delete_purchase_line(token(), purchase.id, removed.id, delivery_action="reallocate", replacement_price_id=replacement.id)
    assert facts() == before
    assert list(purchase.lines.values_list("id", "delivery_share")) == original
    delivery = save_delivery(token(), {"amount": D(1000)})
    before = facts()
    with patch("tseh.services.purchases.audit", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            attach_delivery(token(), delivery, purchase.id, confirm_addition=True)
    assert facts() == before and Purchase.objects.get(pk=purchase.id).delivery_total == 3000


def test_signed_command_tampering_and_stale_correction(state):
    purchase, a, b = setup_purchase()
    stale = token()
    save_delivery(token(), {"amount": D(1)})
    before = facts()
    with pytest.raises(StaleWrite):
        save_purchase(stale, data("4000"), source_rows(purchase), purchase_id=purchase.id, reason="Уточнение")
    with pytest.raises(ValidationError, match="Форма"):
        save_purchase(token() + "tampered", data(), source_rows(purchase), purchase_id=purchase.id)
    assert facts() == before


@pytest.mark.django_db(transaction=True)
def test_concurrent_transfer_only_one_target_changes(state):
    first, _, _ = setup_purchase("0")
    second, _, _ = setup_purchase("0")
    delivery = save_delivery(token(), {"amount": D(3000)})
    stamps, targets = [token(), token()], [first.id, second.id]
    barrier = Barrier(2)
    def worker(index):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            attach_delivery(stamps[index], delivery, targets[index])
            return "saved"
        except StaleWrite:
            return "stale"
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker, [0, 1])) == ["saved", "stale"]
    assert sorted(Purchase.objects.values_list("delivery_total", flat=True)) == [D(0), D(3000)]
    assert AuditEntry.objects.filter(object_type="UnallocatedDelivery", action="transfer").count() == 1
