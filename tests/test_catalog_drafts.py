"""Independent numerical and preservation checks for server-side catalogue drafts."""
from copy import deepcopy
from decimal import Decimal
from io import BytesIO
import hashlib
import uuid

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook
import pytest

from tseh.models import AuditEntry, CatalogDraft, Dish, DomainState, Ingredient, Preparation, Price
from tseh.models.transfer import RestoreSession
from tseh.services import backups
from tseh.services.catalog_drafts import (delete_draft, draft_dependencies, draft_token, preview_draft,
                                          publish_draft, save_draft)
from tseh.services.queries import dish_calculation
from tseh.services.reports import build_xlsx

pytestmark = pytest.mark.django_db


def draft(actor, kind="ingredient", name="Учебный ингредиент", payload=None, **extra):
    return save_draft(actor, token=draft_token(actor, kind), kind=kind, name=name,
                      payload=payload or {}, **extra)


def priced_payload(**extra):
    return {"ingredient_kind": "ingredient", "unit": "kg", "loss": "25", "supplier": "",
            "pack_size": "5", "pack_price": "10000", "price_date": "2026-11-09",
            "zero_price_confirmed": False, **extra}


def line(component, quantity="1000", unit="g", label="Учебный компонент"):
    return {"component": component, "label": label, "quantity": quantity, "unit": unit, "note": ""}


def publish(actor, card):
    preview = preview_draft(actor, card.pk)
    assert preview["valid"], preview["issues"]
    return publish_draft(actor, card.pk, preview["preview_token"])


def restore(actor, document):
    session = backups.prepare_restore(actor, document)
    backups.download_backup(session.pk, actor)
    return backups.apply_restore(session.pk, actor,
        review_token=backups.restore_token(session, actor), confirm_download=True)


def test_name_only_and_ranges_are_saved_without_creating_prices_or_recipes(user, state):
    ingredient = draft(user)
    recipe = draft(user, "dish", "Учебный диапазон", {"base_price": "",
        "lines": [line(f"draft:{ingredient.pk}", quantity="30–50", unit="по вкусу")]},
        source_text="Исходная учебная строка: 30–50", notes="Уточнить количество")
    assert ingredient.payload["pack_price"] == "" and ingredient.payload["loss"] == ""
    assert ingredient.payload["ingredient_kind"] == ""
    assert recipe.payload["lines"][0]["quantity"] == "30–50"
    assert recipe.payload["lines"][0]["unit"] == "по вкусу"
    assert not Ingredient.objects.exists() and not Price.objects.exists()
    assert not Dish.objects.exists() and not Preparation.objects.exists()
    assert not preview_draft(user, recipe.pk)["valid"]
    event = AuditEntry.objects.filter(object_id=str(recipe.pk)).get()
    assert event.actor_name == user.username and event.source == "manual"
    assert event.after["source_text"] == "Исходная учебная строка: 30–50"


def test_complete_dependency_chain_publishes_with_independent_200_tenge_result(user, state):
    raw = draft(user, payload=priced_payload())
    prep = draft(user, "preparation", "Учебная основа", {"output_quantity": "2", "output_unit": "kg",
        "lines": [line(f"draft:{raw.pk}")]})
    dish = draft(user, "dish", "Учебная порция", {"base_price": "700",
        "lines": [line(f"draft:{prep.pk}", quantity="150")]})
    assert not preview_draft(user, prep.pk)["valid"]
    publish(user, raw)
    publish(user, prep)
    before = preview_draft(user, dish.pk)
    # Hand calculation: (10000/5)/(1-.25) / 2 * .150 = 200.
    assert before["result"].total == Decimal("200.00")
    key = publish_draft(user, dish.pk, before["preview_token"])
    result, _assessment = dish_calculation(Dish.objects.get(pk=key))
    assert result.total == Decimal("200.00")
    assert Ingredient.objects.count() == Price.objects.count() == Preparation.objects.count() == Dish.objects.count() == 1
    assert CatalogDraft.objects.filter(published_at__isnull=False).count() == 3
    for card in CatalogDraft.objects.all():
        assert card.version == 2 and card.published_object_id is not None


def test_publish_is_repeatable_after_an_unrelated_draft_save_without_duplicate_writes(user, state):
    card = draft(user, payload=priced_payload())
    preview = preview_draft(user, card.pk)
    first = publish_draft(user, card.pk, preview["preview_token"])
    draft(user, name="Ещё один учебный черновик")
    before = AuditEntry.objects.count()
    assert publish_draft(user, card.pk, preview["preview_token"]) == first
    assert Ingredient.objects.count() == Price.objects.count() == 1
    assert AuditEntry.objects.count() == before


def test_preparation_preview_uses_a_separate_namespace_from_active_object_ids(user, state):
    raw = draft(user, payload=priced_payload(loss="0", pack_size="1", pack_price="100"))
    raw_id = publish(user, raw)
    base = draft(user, "preparation", "Учебная основа 100", {"output_quantity": "1", "output_unit": "kg",
        "lines": [line(f"ingredient:{raw_id}")]})
    base_id = publish(user, base)
    candidate = save_draft(user, token=draft_token(user, "preparation", new_id=base_id), kind="preparation",
        name="Учебный замес 10", payload={"output_quantity": "1", "output_unit": "kg",
            "lines": [line(f"preparation:{base_id}", quantity="100")]})
    preview = preview_draft(user, candidate.pk)
    assert preview["valid"], preview["issues"]
    assert preview["result"].total == Decimal("10.00")
    assert Preparation.objects.get(pk=base_id).name == "Учебная основа 100"


def test_zero_price_is_explicit_and_unknown_price_cannot_become_zero(user, state):
    unknown = draft(user, payload=priced_payload(pack_price=""))
    assert not preview_draft(user, unknown.pk)["valid"]
    free = draft(user, name="Учебный бесплатный образец", payload=priced_payload(pack_price="0"))
    assert not preview_draft(user, free.pk)["valid"]
    free = save_draft(user, token=draft_token(user, free.kind, free), draft_id=free.pk, kind=free.kind,
        name=free.name, payload={**free.payload, "zero_price_confirmed": True})
    key = publish(user, free)
    assert Ingredient.objects.get(pk=key).current_price.pack_price == Decimal("0")
    assert Price.objects.count() == 1


def test_import_can_preassign_ids_and_keep_refs_until_dependency_exists(user, state):
    ingredient_id, prep_id = uuid.uuid4(), uuid.uuid4()
    prep = save_draft(user, token=draft_token(user, "preparation", new_id=prep_id), kind="preparation",
        name="Учебная импортная заготовка", source="import", source_text="Синтетический исходник",
        payload={"lines": [line(f"draft:{ingredient_id}")]})
    assert prep.pk == prep_id and not preview_draft(user, prep.pk)["valid"]
    raw = save_draft(user, token=draft_token(user, "ingredient", new_id=ingredient_id), kind="ingredient",
        name="Учебная импортная позиция", source="import", payload={})
    assert raw.pk == ingredient_id
    assert all(event.source == "import" and event.actor_name == user.username for event in AuditEntry.objects.all())


def test_label_only_dependency_blocks_delete_without_silently_binding(user, state):
    raw = draft(user, name="Учебное сырьё")
    recipe = draft(user, "dish", "Учебный рецепт", {"lines": [line("", label="  УЧЕБНОЕ   СЫРЬЁ  ")]})
    dependencies = draft_dependencies(raw, user)
    assert dependencies == [{"id": recipe.pk, "name": recipe.name, "kind": "dish"}]
    assert recipe.payload["lines"][0]["component"] == ""
    with pytest.raises(ValidationError, match="используется"):
        delete_draft(user, raw.pk, draft_token(user, raw.kind, raw, action="delete"))
    assert CatalogDraft.objects.filter(pk=raw.pk).exists()


def test_v2_backup_restores_incomplete_values_links_source_and_all_timestamps(user, state):
    raw = draft(user, payload={}, source="import", source_text="Учебный исходник\nЦена не указана")
    recipe = draft(user, "dish", "Учебная карточка", {"lines": [line(f"draft:{raw.pk}", "2–3")]},
                   notes="Уточнить", source_text="Учебная карточка 2–3")
    before = backups.export_snapshot()
    assert before["schema_version"] == 2 and len(before["data"]["catalog_drafts"]) == 2
    assert backups.validate_backup(backups.encode_backup(before)) == before
    save_draft(user, token=draft_token(user, recipe.kind, recipe), draft_id=recipe.pk, kind=recipe.kind,
               name="Поздняя правка", payload=recipe.payload)
    restore(user, before)
    after = backups.export_snapshot()
    assert after["data"]["catalog_drafts"] == before["data"]["catalog_drafts"]
    assert not Ingredient.objects.exists() and not Dish.objects.exists()
    assert backups.validate_backup(after) == after


@pytest.mark.parametrize("action", ["save", "delete"])
def test_stale_loaded_draft_cannot_capture_new_epoch_after_same_version_restore(user, state, action):
    loaded_before_restore = draft(user, name="Старая карточка", payload={"pack_price": ""})
    document = backups.export_snapshot()
    row = document["data"]["catalog_drafts"][0]
    row["name"] = "Восстановленная карточка"
    row["payload"]["pack_price"] = "123"
    restore(user, document)
    current = CatalogDraft.objects.get(pk=loaded_before_restore.pk)
    assert current.version == loaded_before_restore.version
    # A GET loaded its model before restore, then signs against the new epoch.
    late_token = draft_token(user, loaded_before_restore.kind, loaded_before_restore, action=action)
    with pytest.raises(ValidationError, match="изменился"):
        if action == "save":
            save_draft(user, token=late_token, draft_id=current.pk, kind=loaded_before_restore.kind,
                       name="Попытка вернуть старое", payload=loaded_before_restore.payload)
        else:
            delete_draft(user, current.pk, late_token)
    current.refresh_from_db()
    assert current.name == "Восстановленная карточка" and current.payload["pack_price"] == "123"


def test_saved_edit_replay_checks_receipt_before_baseline_digest(user, state):
    original = draft(user)
    token = draft_token(user, original.kind, original)
    change = {"token": token, "draft_id": original.pk, "kind": original.kind,
              "name": "Уточнённая карточка", "payload": {**original.payload, "pack_price": "123"}}
    saved = save_draft(user, **change)
    count = AuditEntry.objects.count()
    repeated = save_draft(user, **change)
    assert repeated.pk == saved.pk and repeated.version == saved.version == 2
    assert repeated.payload["pack_price"] == "123"
    assert AuditEntry.objects.count() == count


def test_legacy_v1_and_legacy_restore_counts_remain_accepted(user, state):
    old = backups.export_snapshot()
    old["schema_version"] = 1
    old["data"].pop("catalog_drafts")
    normalized = backups.validate_backup(old)
    assert normalized["schema_version"] == 2 and normalized["data"]["catalog_drafts"] == []
    restore(user, old)
    restored = backups.export_snapshot()
    event = restored["data"]["journal"][-1]
    event["before"]["counts"].pop("catalog_drafts", None)
    event["after"]["counts"].pop("catalog_drafts", None)
    assert backups.validate_backup(restored) == restored


@pytest.mark.parametrize("old_documents", ["proposed", "previous", "both"])
def test_pending_legacy_restore_is_explicitly_rejected_and_original_v1_reuploads(user, authenticated, state, old_documents):
    current = backups.export_snapshot()
    legacy = deepcopy(current)
    legacy["schema_version"] = 1
    legacy["data"].pop("catalog_drafts")
    proposed = legacy if old_documents in {"proposed", "both"} else current
    previous = legacy if old_documents in {"previous", "both"} else current
    pending = RestoreSession.objects.create(owner=user, proposed=proposed, previous=previous,
        source_hash=hashlib.sha256(backups.encode_backup(proposed)).hexdigest(),
        baseline_epoch=state.epoch, baseline_revision=state.revision,
        downloaded_at=timezone.now(), expires_at=timezone.now() + backups.RESTORE_LIFETIME)
    before = list(RestoreSession.objects.filter(pk=pending.pk).values())
    confirmation = backups.restore_token(pending, user)
    message = "Подготовка создана в предыдущей версии. Загрузите исходный JSON заново."
    actions = [
        lambda: backups.get_restore(pending.pk, user),
        lambda: backups.refresh_restore(pending.pk, user),
        lambda: backups.download_backup(pending.pk, user),
        lambda: backups.apply_restore(pending.pk, user, review_token=confirmation, confirm_download=True),
    ]
    for action in actions:
        with pytest.raises(ValidationError, match="предыдущей версии"):
            action()
    response = authenticated.get(reverse("restore-review", args=[pending.pk]))
    assert response.status_code == 400 and message in response.content.decode()
    response = authenticated.post(reverse("restore-refresh", args=[pending.pk]))
    assert response.status_code == 400 and message in response.content.decode()
    response = authenticated.post(reverse("restore-review", args=[pending.pk]),
                                  {"review_token": confirmation, "confirm_download": "on"})
    assert response.status_code == 400 and message in response.content.decode()
    assert list(RestoreSession.objects.filter(pk=pending.pk).values()) == before
    assert backups.export_snapshot()["data"] == current["data"]

    upload = SimpleUploadedFile("legacy.json", backups.encode_backup(legacy), content_type="application/json")
    response = authenticated.post(reverse("exchange"), {"backup_file": upload})
    assert response.status_code == 302
    fresh = RestoreSession.objects.exclude(pk=pending.pk).get()
    assert fresh.proposed["schema_version"] == fresh.previous["schema_version"] == 2
    assert authenticated.get(reverse("restore-review", args=[fresh.pk])).status_code == 200
    backups.download_backup(fresh.pk, user)
    backups.apply_restore(fresh.pk, user, review_token=backups.restore_token(fresh, user), confirm_download=True)
    fresh.refresh_from_db()
    assert fresh.completed_at is not None


def test_completed_legacy_restore_receipt_remains_viewable_without_rewriting(user, authenticated, state):
    legacy = backups.export_snapshot()
    legacy["schema_version"] = 1
    legacy["data"].pop("catalog_drafts")
    receipt = RestoreSession.objects.create(owner=user, proposed=legacy, previous=legacy,
        source_hash=hashlib.sha256(backups.encode_backup(legacy)).hexdigest(), baseline_epoch=state.epoch,
        baseline_revision=state.revision, completed_at=timezone.now(),
        expires_at=timezone.now() + backups.RESTORE_LIFETIME)
    assert backups.get_restore(receipt.pk, user).previous == legacy
    response = authenticated.get(reverse("restore-review", args=[receipt.pk]))
    assert response.status_code == 200
    receipt.refresh_from_db()
    assert receipt.proposed == legacy and receipt.previous == legacy


@pytest.mark.parametrize("damage", ["payload_extra", "payload_missing", "receipt", "source", "journal_payload"])
def test_backup_rejects_malformed_draft_rows_and_audit_snapshots(user, state, damage):
    draft(user)
    bad = deepcopy(backups.export_snapshot())
    row = bad["data"]["catalog_drafts"][0]
    if damage == "payload_extra": row["payload"]["execute"] = "unsafe"
    if damage == "payload_missing": row["payload"].pop("pack_price")
    if damage == "receipt": row["published_object_id"] = str(uuid.uuid4())
    if damage == "source": row["source"] = "unknown"
    if damage == "journal_payload": bad["data"]["journal"][0]["after"]["payload"]["pack_price"] = 0
    with pytest.raises(ValidationError):
        backups.validate_backup(bad)


def test_publication_receipt_survives_target_purge_in_backup(user, state):
    card = draft(user, "dish", "Учебная историческая карточка", {})
    # Model a retained historical receipt after an independently purged target.
    from django.utils import timezone
    now = timezone.now()
    CatalogDraft.objects.filter(pk=card.pk).update(published_object_id=uuid.uuid4(), published_at=now,
                                                 updated_at=now, version=2)
    document = backups.export_snapshot()
    assert backups.validate_backup(document) == document
    restore(user, document)
    card.refresh_from_db()
    assert card.is_published and not Dish.objects.filter(pk=card.published_object_id).exists()


def test_xlsx_preserves_draft_text_and_does_not_treat_unknown_price_as_zero(user, state):
    raw = draft(user, name="=Учебное сырьё", source_text="=HYPERLINK(\"example\")", notes="Проверить")
    draft(user, "dish", "Учебная строка", {"lines": [line(f"draft:{raw.pk}", "30–50", label="=Сырьё")]})
    book = load_workbook(BytesIO(build_xlsx()), data_only=False)
    assert len(book.sheetnames) == 15
    cards = book["Черновики"]
    headers = {cell.value: cell.column for cell in cards[5]}
    raw_row = next(row for row in cards.iter_rows(min_row=6) if row[0].value == raw.name)
    assert raw_row[headers["Цена упаковки: ввод"] - 1].value is None
    assert raw_row[0].data_type == "s"
    rows = book["Состав черновиков"]
    assert rows.cell(6, 6).value == "30–50" and rows.cell(6, 6).data_type == "s"
    assert rows.cell(6, 4).value == "=Сырьё" and rows.cell(6, 4).data_type == "s"
    source = book["Исходники черновиков"]
    raw_source = next(row for row in source.iter_rows(min_row=6) if row[0].value == raw.name)
    assert raw_source[3].value == '=HYPERLINK("example")' and raw_source[3].data_type == "s"
