from copy import deepcopy
from datetime import datetime, timedelta, timezone as tz
from decimal import Decimal as D
from unittest.mock import patch
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError

from tseh.models import (AuditEntry, Dish, DomainState, GlobalSettings, Ingredient,
    Operation, Price, PriceApplication, Purchase, PurchaseLine, UnallocatedDelivery)
from tseh.models.transfer import RestoreSession
from tseh.services import backups
from tseh.services.commands import save_dish, save_supplier, save_thresholds, update_ingredient
from tseh.services.lifecycle import purge_due_dishes, transition_catalog, transition_dish
from tseh.services.prices import apply_price, create_price
from tseh.services.purchases import delete_purchase_line, save_delivery
from tseh.services.queries import dish_calculation
from tests.test_purchase_web import line_input, purchase
from tests.test_stage2_services import item, line, prep, token

pytestmark = pytest.mark.django_db
T = datetime(2026, 11, 5, 10, 0, tzinfo=tz.utc)


def snapshot():
    return backups.export_snapshot(now=T)


def ready(user, document):
    prepared = backups.prepare_restore(user, document)
    backups.download_backup(prepared.pk, user)
    return prepared


def apply(user, document):
    prepared = ready(user, document)
    return submit(prepared.pk, user, confirm_download=True)


def submit(session_id, user, *, confirm_download=False, review_token=None):
    if review_token is None:
        review_token = backups.restore_token(backups.get_restore(session_id, user), user)
    return backups.apply_restore(session_id, user, review_token=review_token, confirm_download=confirm_download)


def basic():
    key = item(loss="25", price="2000")
    dish = save_dish(token(), {"name": "Эталон", "base_price": D(1000)}, [line(key, "0.1")])
    return key, dish


def test_complete_roundtrip_preserves_history_precision_journal_auth_and_epoch(user, authenticated, state):
    key, dish = basic()
    original = Ingredient.objects.get(pk=key).current_price_id
    offer = create_price(token(), key, {"supplier_id": None, "price_date": "2026-11-01",
        "pack_size": D("0.000000000001"), "pack_price": D("0.01"), "comment": "Не применена", "is_one_off": True})
    other = Price.objects.filter(ingredient_id=key).exclude(pk=original).get()
    apply_price(token(), key, other.pk)
    apply_price(token(), key, original)
    save_delivery(token(), {"amount": D("123.45"), "note": "Отдельный расход"})
    a = prep("Вложенная основа", [line(key, "0.000000000000123")], output="0.000000000002")
    prep("Внешняя основа", [line(a, "0.000000000000111", prep=True)], output="0.000000000004")
    save_thresholds(token(), {"green": D(40), "orange": D(50)})
    save_dish(token(), {"name": "Эталон", "base_price": D(1000), "green_override": D(45), "orange_override": None},
        [line(key, "0.1")], dish)
    # Subsequent global change legitimately creates an inherited conflict.
    save_thresholds(token(), {"green": D(30), "orange": D(40)})
    before = snapshot()
    assert backups.validate_backup(backups.encode_backup(before)) == before
    old_epoch = DomainState.objects.get(pk=1).epoch
    auth_before = list(get_user_model().objects.values())
    sessions_before = list(Session.objects.values())
    old_token = token()
    update_ingredient(token(), key, {"name": "Поздняя правка", "loss": D(0)})
    receipt = apply(user, before)
    after = snapshot()
    for table in backups.TABLES:
        if table != "journal":
            assert after["data"][table] == before["data"][table], table
    assert after["data"]["journal"][:-1] == before["data"]["journal"]
    restored_event = after["data"]["journal"][-1]
    assert restored_event["source"] == "import" and restored_event["action"] == "restore"
    assert restored_event["group"] == str(receipt.pk)
    assert DomainState.objects.get(pk=1).epoch != old_epoch and not Operation.objects.exists()
    assert list(get_user_model().objects.values()) == auth_before
    assert list(Session.objects.values()) == sessions_before
    assert dish_calculation(Dish.objects.get(pk=dish))[0].total == D("266.67")
    assert Dish.objects.get(pk=dish).green_override == D(45)
    assert backups.validate_backup(after) == after  # includes a historical restore event
    with pytest.raises(ValidationError, match="восстановлены"):
        update_ingredient(old_token, key, {"name": "Старая форма", "loss": D(0)})


def test_purchase_preserves_aggregates_and_deliberately_saved_delivery_shares(user, state):
    a, b, c = item(), item(), item()
    pk = purchase(line_input(a, size="1", price="1", count="1"),
                  line_input(b, size="1", price="1", count="1"),
                  line_input(c, size="1", price="1", count="1"), delivery="1")
    first = PurchaseLine.objects.get(purchase_id=pk, position=0)
    delete_purchase_line(token(), pk, first.pk, delivery_action="drop_share")
    before = snapshot()
    assert [row["delivery_share"] for row in before["data"]["purchase_lines"]] == ["0.33", "0.33"]
    apply(user, before)
    assert Purchase.objects.get(pk=pk).delivery_total == D("0.66")
    assert list(PurchaseLine.objects.filter(purchase_id=pk).values_list("delivery_share", flat=True)) == [D("0.33"), D("0.33")]
    after = snapshot()
    for name in ("purchases", "purchase_lines", "prices"):
        assert after["data"][name] == before["data"][name]


@pytest.mark.parametrize("remaining", [12 * 86400 * 1_000_000, (2 * 3600 + 30 * 60) * 1_000_000, 1, 0])
def test_trash_restores_exact_remaining_duration_not_old_date_or_new_30_days(user, state, remaining):
    _, dish = basic()
    trashed_at = datetime(2026, 10, 1, 12, 0, tzinfo=tz.utc)
    transition_dish(token(), dish, "archive", now=trashed_at - timedelta(days=20))
    transition_dish(token(), dish, "trash", now=trashed_at)
    snap_at = trashed_at + timedelta(days=30) - timedelta(microseconds=remaining)
    document = backups.export_snapshot(now=snap_at)
    assert document["data"]["dishes"][0]["remaining_microseconds"] == remaining
    restore_at = datetime(2026, 11, 19, 12, 0, tzinfo=tz.utc)
    with patch("tseh.services.backups.timezone.now", return_value=restore_at):
        receipt = apply(user, document)
        restored = Dish.objects.get(pk=dish)
        assert restored.trashed_at == trashed_at
        assert restored.purge_at == restore_at + timedelta(microseconds=remaining)
        entry = AuditEntry.objects.get(group=receipt.pk, source="import")
        assert entry.after["trash_deadlines"][0]["remaining_microseconds"] == remaining
        assert backups.export_snapshot()["data"]["dishes"][0]["remaining_microseconds"] == remaining
        if remaining:
            assert purge_due_dishes(now=restored.purge_at - timedelta(microseconds=1)) == 0
        assert purge_due_dishes(now=restored.purge_at) == 1
    assert AuditEntry.objects.filter(object_id=str(dish), action="purge").exists()


def test_stale_prebackup_requires_fresh_download_and_confirmation(user, state):
    key, _ = basic()
    prepared = ready(user, snapshot())
    update_ingredient(token(), key, {"name": "Новая запись", "loss": D(0)})
    before = snapshot()
    with pytest.raises(ValidationError, match="свежую копию"):
        submit(prepared.pk, user, confirm_download=True)
    assert snapshot() == before
    with pytest.raises(ValidationError, match="свежую копию"):
        backups.download_backup(prepared.pk, user)
    refreshed = backups.refresh_restore(prepared.pk, user)
    assert refreshed.downloaded_at is None
    assert refreshed.previous["data"] == before["data"]
    with pytest.raises(ValidationError, match="Сначала скачайте"):
        submit(prepared.pk, user, confirm_download=True)
    backups.download_backup(prepared.pk, user)
    submit(prepared.pk, user, confirm_download=True)
    assert Ingredient.objects.get(pk=key).name == "X"


def test_download_owner_expiry_and_strict_confirmation(user, state):
    basic()
    prepared = backups.prepare_restore(user, snapshot())
    other = get_user_model().objects.create_user(username="other", password="test-strong-password")
    for bad in (False, 1, "yes", None):
        with pytest.raises(ValidationError):
            submit(prepared.pk, user, confirm_download=bad)
    with pytest.raises(ValidationError):
        backups.get_restore(prepared.pk, other)
    with pytest.raises(ValidationError):
        backups.download_backup(prepared.pk, other)
    with pytest.raises(ValidationError):
        submit(prepared.pk, user, confirm_download=True)
    backups.download_backup(prepared.pk, user)
    RestoreSession.objects.filter(pk=prepared.pk).update(expires_at=T - timedelta(days=1))
    with pytest.raises(ValidationError, match="истекла"):
        submit(prepared.pk, user, confirm_download=True)
    assert backups.cleanup_restore_sessions(now=T) == 1
    assert backups.cleanup_restore_sessions(now=T) == 0


def test_replay_does_not_restore_again_or_duplicate_import_event(user, state):
    key, _ = basic()
    receipt = apply(user, snapshot())
    update_ingredient(token(), key, {"name": "После восстановления", "loss": D(0)})
    before = snapshot()
    result = submit(receipt.pk, user, confirm_download=True)
    assert result.pk == receipt.pk and snapshot() == before
    assert AuditEntry.objects.filter(source="import").count() == 1


def test_failure_after_replacement_rolls_back_domain_and_receipt(user, state):
    key, _ = basic()
    document = snapshot()
    update_ingredient(token(), key, {"name": "Не потерять", "loss": D(0)})
    prepared = ready(user, document)
    before = snapshot()
    real_replace = backups._replace
    def fail_after(tables):
        real_replace(tables)
        raise ValidationError("Искусственная ошибка после замены")
    with patch("tseh.services.backups._replace", side_effect=fail_after):
        with pytest.raises(ValidationError, match="после замены"):
            submit(prepared.pk, user, confirm_download=True)
    assert snapshot() == before
    assert RestoreSession.objects.get(pk=prepared.pk).completed_at is None
    # Sequence increments during a rollback must not make later inserts collide.
    save_supplier(token(), {"name": "После отката", "note": ""})
    assert AuditEntry.objects.filter(object_name="После отката").exists()


def test_failed_preimport_copy_does_not_create_staging_or_change_domain(user, state):
    basic()
    document = snapshot()
    with patch("tseh.services.backups._export_locked", side_effect=ValidationError("Ошибка копии")):
        with pytest.raises(ValidationError, match="Ошибка копии"):
            backups.prepare_restore(user, document)
    assert not RestoreSession.objects.exists()
    assert snapshot() == document


@pytest.mark.parametrize("damage", ["version", "missing-section", "unknown-field", "missing-link", "wrong-current-owner",
    "missing-application", "wrong-number-type", "precision", "negative", "boolean-number", "duplicate-row",
    "unit", "empty-recipe", "bad-journal", "secret", "bad-snapshot"])
def test_corrupt_file_rejected_without_writes(user, state, damage):
    key, dish = basic()
    second = item()
    document = snapshot()
    bad = deepcopy(document)
    if damage == "version": bad["schema_version"] = 99
    elif damage == "missing-section": del bad["data"]["suppliers"]
    elif damage == "unknown-field": bad["data"]["ingredients"][0]["surprise"] = True
    elif damage == "missing-link": bad["data"]["ingredients"][0]["current_price_id"] = str(uuid.uuid4())
    elif damage == "wrong-current-owner": bad["data"]["ingredients"][0]["current_price_id"] = bad["data"]["ingredients"][1]["current_price_id"]
    elif damage == "missing-application": bad["data"]["price_applications"] = []
    elif damage == "wrong-number-type": bad["data"]["prices"][0]["pack_price"] = 100
    elif damage == "precision": bad["data"]["prices"][0]["pack_price"] = "1.001"
    elif damage == "negative": bad["data"]["prices"][0]["pack_price"] = "-1.00"
    elif damage == "boolean-number": bad["data"]["dish_lines"][0]["position"] = True
    elif damage == "duplicate-row": bad["data"]["dishes"].append(deepcopy(bad["data"]["dishes"][0]))
    elif damage == "unit": bad["data"]["dish_lines"][0]["unit"] = "ml"
    elif damage == "empty-recipe": bad["data"]["dish_lines"] = []
    elif damage == "bad-journal": bad["data"]["journal"][0]["before"] = []
    elif damage == "secret": bad["data"]["journal"][0]["after"]["password"] = "do-not-accept"
    elif damage == "bad-snapshot": bad["data"]["journal"][0]["after"]["arbitrary_schema"] = "wrong"
    with pytest.raises(ValidationError):
        backups.prepare_restore(user, bad)
    assert snapshot() == document and not RestoreSession.objects.exists()


def test_cycles_delivery_mismatch_and_snapshot_duration_rejected(state):
    key, dish = basic()
    a = prep("A", [line(key)])
    b = prep("B", [line(a, prep=True)])
    pk = purchase(line_input(key))
    document = snapshot()
    cyclic = deepcopy(document)
    row = next(row for row in cyclic["data"]["preparation_lines"] if row["parent_id"] == str(a))
    row.update(ingredient_id=None, preparation_id=str(b))
    with pytest.raises(ValidationError, match="Цикл"):
        backups.validate_backup(cyclic)
    broken = deepcopy(document)
    broken["data"]["purchase_lines"][0]["delivery_share"] = "0.00"
    with pytest.raises(ValidationError, match="Сумма долей"):
        backups.validate_backup(broken)
    broken = deepcopy(document)
    next(row for row in broken["data"]["prices"] if row["purchase_id"])["goods_total"] = "1"
    with pytest.raises(ValidationError, match="Итоги цены"):
        backups.validate_backup(broken)
    transition_dish(token(), dish, "archive", now=T)
    transition_dish(token(), dish, "trash", now=T)
    broken = snapshot()
    broken["data"]["dishes"][0]["remaining_microseconds"] -= 1
    with pytest.raises(ValidationError, match="Остаток срока"):
        backups.validate_backup(broken)


@pytest.mark.parametrize("source", [b'{"format":1,"format":2}', b'{"x":NaN}', b'{"x":1.5}', b'not-json', b'\xff'])
def test_json_parser_rejects_duplicates_nonfinite_float_and_bad_encoding(source):
    with pytest.raises(ValidationError):
        backups.validate_backup(source)


def test_json_limits_and_no_export_of_auth_or_internal_state(user, authenticated, state):
    basic()
    document = snapshot()
    assert set(document["data"]) == set(backups.TABLES)
    assert not {"auth", "sessions", "operations", "restore_sessions", "domain_state"}.intersection(document["data"])
    encoded = backups.encode_backup(document)
    assert b'password' not in encoded and b'csrfmiddlewaretoken' not in encoded
    with pytest.raises(ValidationError, match="размер"):
        backups.validate_backup(b' ' * (backups.MAX_BACKUP_BYTES + 1))
    deep = value = {}
    for _ in range(backups.MAX_JSON_DEPTH + 2):
        value["x"] = {}
        value = value["x"]
    with pytest.raises(ValidationError, match="вложенности"):
        backups.validate_backup(deep)


@pytest.mark.parametrize("key,value", [("id", {}), ("kind", []), ("unit", {}), ("status", []),
    ("delivery_action", []), ("source_hash", {}), ("name", []), ("name", None), ("is_one_off", "yes")])
def test_corrupt_snapshot_types_are_validation_errors_not_server_errors(state, key, value):
    basic()
    bad = snapshot()
    bad["data"]["journal"][0]["after"][key] = value
    with pytest.raises(ValidationError):
        backups.validate_backup(bad)


def test_deleted_object_snapshot_is_valid_without_live_reference(user, state):
    basic()
    removed_id = uuid.uuid4()
    AuditEntry.objects.create(group=uuid.uuid4(), object_type="Supplier", object_id=str(removed_id),
        object_name="Удалённый поставщик", action="delete", source="manual",
        before={"name": "Удалённый поставщик", "note": "Историческое значение"}, after=None)
    document = snapshot()
    apply(user, document)
    assert AuditEntry.objects.filter(object_id=str(removed_id), action="delete").exists()


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59"])
def test_timestamp_must_be_representable_in_utc_before_database_write(state, value):
    basic()
    bad = snapshot()
    bad["data"]["prices"][0]["created_at"] = value
    with pytest.raises(ValidationError, match="диапазон UTC"):
        backups.validate_backup(bad)


def test_unpaired_unicode_surrogates_are_rejected():
    with pytest.raises(ValidationError, match="Unicode"):
        backups.validate_backup(b'{"x":"\\ud800"}')


def test_export_never_returns_a_file_exceeding_import_limits(state):
    basic()
    good = snapshot()
    assert backups.validate_backup(good) == good
    with patch("tseh.services.backups.MAX_BACKUP_BYTES", 100):
        with pytest.raises(ValidationError, match="размер"):
            snapshot()


def test_indefinite_catalog_trash_roundtrips_and_permanent_purge_journal_remains_valid(user, state):
    basic()
    unused_supplier = save_supplier(token(), {"name": "Неиспользуемый поставщик", "note": ""})
    unused_ingredient = item()
    raw = item()
    unused_prep = prep("Неиспользуемая заготовка", [line(raw)])
    for kind, key in (("supplier", unused_supplier), ("ingredient", unused_ingredient), ("preparation", unused_prep)):
        transition_catalog(token(), kind, key, "trash", now=T)
    document = snapshot()
    for name in ("suppliers", "ingredients", "preparations"):
        trashed = [row for row in document["data"][name] if row["status"] == "trashed"]
        assert len(trashed) == 1 and "remaining_microseconds" not in trashed[0] and "purge_at" not in trashed[0]
    apply(user, document)
    assert Ingredient.objects.get(pk=unused_ingredient).status == "trashed"
    transition_catalog(token(), "ingredient", unused_ingredient, "purge")
    purged = snapshot()  # exported journal includes historical prices and applications
    assert not Ingredient.objects.filter(pk=unused_ingredient).exists()
    apply(user, purged)
    entry = AuditEntry.objects.get(object_id=str(unused_ingredient), action="purge")
    assert len(entry.before["prices"]) == 1 and len(entry.before["applications"]) == 1


@pytest.mark.parametrize("name", ["suppliers", "ingredients", "preparations"])
def test_catalog_trash_cannot_have_external_references(state, name):
    key, dish = basic()
    prep_key = prep("Используется", [line(key)])
    save_dish(token(), {"name": "Эталон", "base_price": D(1000)}, [line(prep_key, prep=True)], dish)
    document = snapshot()
    record = document["data"][name][0]
    record.update(status="trashed", trashed_at=T.isoformat())
    with pytest.raises(ValidationError, match="корзине"):
        backups.validate_backup(document)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("same_receipt", [True, False])
def test_concurrent_restore_is_single_atomic_replacement(user, state, same_receipt):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from django.db import close_old_connections, connections
    basic()
    document = snapshot()
    first = ready(user, document)
    second = first if same_receipt else ready(user, document)
    barrier = Barrier(2)
    def execute(pk):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                return str(submit(pk, user, confirm_download=True).pk)
            except ValidationError:
                return "stale"
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, [first.pk, second.pk]))
    assert AuditEntry.objects.filter(source="import").count() == 1
    assert RestoreSession.objects.filter(completed_at__isnull=False).count() == 1
    assert results.count("stale") == (0 if same_receipt else 1)


@pytest.mark.parametrize("change_domain", [True, False])
def test_refresh_in_another_tab_invalidates_old_review_even_after_new_download(user, state, change_domain):
    key, _ = basic()
    prepared = ready(user, snapshot())
    old_review = backups.restore_token(prepared, user)
    if change_domain:
        update_ingredient(token(), key, {"name": "Другая вкладка изменила данные", "loss": D(0)})
    refreshed = backups.refresh_restore(prepared.pk, user)
    assert refreshed.previous["snapshot_at"] != prepared.previous["snapshot_at"]
    backups.download_backup(prepared.pk, user)
    before = snapshot()
    with pytest.raises(ValidationError, match="подтверждение изменились"):
        submit(prepared.pk, user, confirm_download=True, review_token=old_review)
    assert snapshot() == before
    assert RestoreSession.objects.get(pk=prepared.pk).completed_at is None
    new_review = backups.restore_token(refreshed, user)
    submit(prepared.pk, user, confirm_download=True, review_token=new_review)
    assert Ingredient.objects.get(pk=key).name == "X"


def test_review_binds_session_user_source_and_copy_and_validates_replay(user, state):
    from django.core import signing
    basic()
    document = snapshot()
    first, second = ready(user, document), ready(user, document)
    original = backups.restore_token(first, user)
    wrong_session = backups.restore_token(second, user)
    before = snapshot()
    for bad in ("", original + "x", wrong_session):
        with pytest.raises(ValidationError):
            submit(first.pk, user, confirm_download=True, review_token=bad)
    payload = signing.loads(original, salt=backups.REVIEW_SALT)
    for field, replacement in (("user", "someone-else"), ("source_hash", "0" * 64),
            ("previous_hash", "0" * 64), ("epoch", str(uuid.uuid4())), ("revision", -1)):
        modified = {**payload, field: replacement}
        token_value = signing.dumps(modified, salt=backups.REVIEW_SALT)
        with pytest.raises(ValidationError):
            submit(first.pk, user, confirm_download=True, review_token=token_value)
    assert snapshot() == before
    # Download does not invalidate the displayed confirmation token.
    backups.download_backup(first.pk, user)
    submit(first.pk, user, confirm_download=True, review_token=original)
    completed = snapshot()
    with pytest.raises(ValidationError):
        submit(first.pk, user, confirm_download=True, review_token=original + "bad")
    assert snapshot() == completed
    submit(first.pk, user, confirm_download=True, review_token=original)
    assert snapshot() == completed
