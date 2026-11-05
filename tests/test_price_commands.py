from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from decimal import Decimal as D
from threading import Barrier
from unittest.mock import patch
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections, transaction, IntegrityError

from tseh.models import AuditEntry, DomainState, Ingredient, Operation, Price, PriceApplication
from tseh.services.commands import StaleWrite
from tseh.services.prices import apply_price, correct_price, create_price, delete_price
from tests.test_stage2_services import item, token

pytestmark = pytest.mark.django_db


def data(amount="2200", **extra):
    return {"pack_price": D(amount), "pack_size": D(1), "price_date": date(2026, 11, 4),
            "comment": "Предложение", "is_one_off": True, **extra}


def offer(ingredient, amount="2200", apply=False):
    prior = set(Price.objects.values_list("pk", flat=True))
    assert create_price(token(), ingredient, data(amount), apply=apply) == ingredient
    return Price.objects.exclude(pk__in=prior).get().pk


def facts():
    return (DomainState.objects.get(pk=1).revision, AuditEntry.objects.count(),
            Operation.objects.count(), Price.objects.count(), PriceApplication.objects.count())


def test_offer_nonmutating_and_apply_repeat_correction(state):
    key = item(price="2000")
    first = Ingredient.objects.get(pk=key).current_price_id
    payload = data()
    original = deepcopy(payload)
    before_events = PriceApplication.objects.count()
    assert create_price(token(), key, payload) == key
    assert payload == original
    second = Price.objects.exclude(pk=first).get().pk
    assert Ingredient.objects.get(pk=key).current_price_id == first
    assert PriceApplication.objects.count() == before_events
    assert apply_price(token(), key, second) == key
    assert apply_price(token(), key, first) == key
    events = list(PriceApplication.objects.values_list("id", "applied_at", "price_id"))
    correction = {"pack_price": D(2100)}
    assert correct_price(token(), key, second, correction) == key
    assert correction == {"pack_price": D(2100)}
    assert list(PriceApplication.objects.values_list("id", "applied_at", "price_id")) == events
    assert list(PriceApplication.objects.values_list("price__pack_price", flat=True)) == [D(2000), D(2100), D(2000)]
    log = AuditEntry.objects.filter(object_type="Price", action="update").get()
    assert log.before["pack_price"] == "2200.00" and log.after["pack_price"] == "2100"
    assert log.before["applications"][0]["id"] == events[1][0]
    assert log.after["reason"] == ""


def test_current_apply_rejected_and_original_confirmation_replayed(state):
    key = item()
    current = Ingredient.objects.get(pk=key).current_price_id
    before = facts()
    with pytest.raises(ValidationError, match="уже действует"):
        apply_price(token(), key, current)
    assert facts() == before
    second = offer(key)
    stamp = token()
    apply_price(stamp, key, second)
    after = facts()
    assert apply_price(stamp, key, second) == key
    assert facts() == after


@pytest.mark.parametrize("action", ["create", "correct", "replace"])
def test_explicit_zero_required_at_command_boundary(state, action):
    key = item()
    current = Ingredient.objects.get(pk=key).current_price_id
    def run(stamp, confirmed):
        if action == "create":
            return create_price(stamp, key, data("0"), confirm_zero=confirmed)
        if action == "correct":
            return correct_price(stamp, key, current, {"pack_price": D(0)}, confirm_zero=confirmed)
        return delete_price(stamp, key, current, replacement_data=data("0"), confirm_zero=confirmed)
    before = facts()
    for confirmed in [False, "on", 1]:
        with pytest.raises(ValidationError):
            run(token(), confirmed)
        assert facts() == before
    stamp = token()
    assert run(stamp, True) == key
    with pytest.raises(StaleWrite):
        run(stamp, False)  # Confirmation is part of the fingerprint.


@pytest.mark.parametrize("bad", [{"ingredient_id": uuid.uuid4()}, {"pack_price": D(-1)},
    {"pack_size": D(0)}, {"pack_price": D("1.005")}, {"supplier_id": uuid.uuid4()},
    {"pack_price": 1.2}, {"is_one_off": "false"}, {"comment": "x" * 2001}])
def test_price_validation_and_ownership_no_partial_changes(state, bad):
    key = item()
    current = Ingredient.objects.get(pk=key).current_price_id
    before = facts()
    with pytest.raises(ValidationError):
        correct_price(token(), key, current, bad)
    assert facts() == before


def test_related_missing_and_foreign_prices_are_validation_errors(state):
    first, second = item(), item()
    own = Ingredient.objects.get(pk=first).current_price_id
    foreign = Ingredient.objects.get(pk=second).current_price_id
    before = facts()
    for key, price in [(first, foreign), (first, uuid.uuid4()), (uuid.uuid4(), own)]:
        for action in [lambda: apply_price(token(), key, price),
                       lambda: correct_price(token(), key, price, {}),
                       lambda: delete_price(token(), key, price)]:
            with pytest.raises(ValidationError):
                action()
    for replacement in [foreign, own, uuid.uuid4()]:
        with pytest.raises(ValidationError):
            delete_price(token(), first, own, replacement_price_id=replacement)
    with pytest.raises(ValidationError):
        delete_price(token(), first, own)
    assert facts() == before


def test_delete_noncurrent_removes_all_events_preserves_original_snapshots(state):
    key = item(price="2000")
    first = Ingredient.objects.get(pk=key).current_price_id
    second = offer(key, apply=True)
    apply_price(token(), key, first)
    apply_price(token(), key, second)
    apply_price(token(), key, first)
    removed = list(PriceApplication.objects.filter(price_id=second).values_list("id", "applied_at"))
    stamp = token()
    assert delete_price(stamp, key, second) == key
    assert not Price.objects.filter(pk=second).exists()
    assert not PriceApplication.objects.filter(price_id=second).exists()
    assert PriceApplication.objects.filter(price_id=first).count() == 3
    log = AuditEntry.objects.get(object_type="Price", object_id=str(second), action="delete")
    assert [(row["id"], row["applied_at"]) for row in log.before["applications"]] == [(pk, str(at)) for pk, at in removed]
    assert log.before["ingredient_name"] and log.before["supplier_name"] is None
    assert all(row["price"]["pack_price"] == "2200.00" for row in log.before["applications"])
    after = facts()
    assert delete_price(stamp, key, second) == key
    assert facts() == after


@pytest.mark.parametrize("new", [False, True])
def test_current_delete_atomic_replacement_and_group(state, new):
    key = item()
    old = Ingredient.objects.get(pk=key).current_price_id
    replacement = offer(key) if not new else None
    kwargs = {"replacement_data": data()} if new else {"replacement_price_id": replacement}
    before_logs = set(AuditEntry.objects.values_list("id", flat=True))
    delete_price(token(), key, old, **kwargs)
    ingredient = Ingredient.objects.get(pk=key)
    assert ingredient.current_price_id != old
    assert ingredient.current_price.ingredient_id == key
    assert ingredient.current_price.pack_price == D(2200)
    assert PriceApplication.objects.get().price_id == ingredient.current_price_id
    logs = AuditEntry.objects.exclude(pk__in=before_logs)
    assert len(set(logs.values_list("group", flat=True))) == 1


@pytest.mark.parametrize("operation", ["create", "apply", "correct", "delete"])
def test_audit_failure_rolls_back_all_price_changes(state, operation):
    key = item()
    old = Ingredient.objects.get(pk=key).current_price_id
    second = offer(key)
    before = facts()
    actions = {"create": lambda: create_price(token(), key, data(), apply=True),
        "apply": lambda: apply_price(token(), key, second),
        "correct": lambda: correct_price(token(), key, old, data()),
        "delete": lambda: delete_price(token(), key, old, replacement_data=data())}
    with patch("tseh.services.prices.audit", side_effect=RuntimeError("journal unavailable")):
        with pytest.raises(RuntimeError):
            actions[operation]()
    assert facts() == before
    assert Ingredient.objects.get(pk=key).current_price_id == old
    assert Price.objects.get(pk=old).pack_price == D(1000)


@pytest.mark.django_db(transaction=True)
def test_competing_price_choices_only_one_applies(state):
    key = item()
    prices = [offer(key, "2100"), offer(key, "2200")]
    tokens = [token(), token()]
    barrier = Barrier(2)
    before_events = PriceApplication.objects.count()
    def worker(index):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            apply_price(tokens[index], key, prices[index])
            return "saved"
        except StaleWrite:
            return "stale"
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker, [0, 1])) == ["saved", "stale"]
    assert PriceApplication.objects.count() == before_events + 1


@pytest.mark.django_db(transaction=True)
def test_foreign_current_price_remains_db_protected(state):
    a, b = item(), item()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Ingredient.objects.filter(pk=a).update(current_price_id=Ingredient.objects.get(pk=b).current_price_id)
