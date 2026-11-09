from decimal import Decimal as D
from unittest.mock import patch
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from tseh.models import AuditEntry, Dish, DomainState, Ingredient, RestoreSession, Supplier
from tseh.services import backups
from tseh.services.commands import save_dish, save_supplier, update_ingredient
from tseh.services.lifecycle import transition_catalog, transition_dish
from tests.test_stage2_services import item, line, prep, token

pytestmark = pytest.mark.django_db


def facts():
    state = DomainState.objects.get(pk=1)
    return (state.epoch, state.revision, AuditEntry.objects.count(),
        list(Ingredient.objects.order_by("id").values()), list(Dish.objects.order_by("id").values()))


def upload(client, document):
    return client.post(reverse("exchange"), {"backup_file": SimpleUploadedFile("copy.json", backups.encode_backup(document), "application/json")})


def confirmation(client, session):
    response = client.get(reverse("restore-review", args=[session.pk]))
    return {"review_token": response.context["form"]["review_token"].value(), "confirm_download": "on"}


def transition(client, kind, key, action):
    url = reverse("dish-transition", args=[key, action]) if kind == "dish" else reverse("catalog-transition", args=[kind, key, action])
    before = facts()
    response = client.get(url)
    assert response.status_code == 200 and facts() == before
    posted = {"token": response.context["form"]["token"].value()}
    result = client.post(url, posted)
    return result, posted, url


def test_active_uniqueness_and_full_dish_archive_trash_return_path(authenticated, state):
    common, rare = item(), item()
    a = save_dish(token(), {"name": "Первое", "base_price": D(500)}, [line(common), line(rare)])
    b = save_dish(token(), {"name": "Второе", "base_price": D(500)}, [line(common)])
    assert authenticated.get(reverse("dish-detail", args=[a])).context["uniqueness"].percentage == 50
    response, posted, url = transition(authenticated, "dish", b, "archive")
    assert response.status_code == 302
    assert authenticated.get(reverse("dish-detail", args=[a])).context["uniqueness"].percentage == 100
    assert authenticated.get(reverse("dashboard")).context["dish_count"] == 1
    before = facts()
    assert authenticated.post(url, posted).status_code == 302 and facts() == before
    assert transition(authenticated, "dish", b, "trash")[0].status_code == 302
    assert authenticated.get(reverse("dish-edit", args=[b])).status_code == 302
    assert transition(authenticated, "dish", b, "restore")[0].status_code == 302
    assert Dish.objects.get(pk=b).status == "archived"
    assert transition(authenticated, "dish", b, "activate")[0].status_code == 302
    assert authenticated.get(reverse("dish-detail", args=[a])).context["uniqueness"].percentage == 50


def test_catalog_trash_all_types_search_filter_sort_restore_and_purge(authenticated, state):
    raw = item()
    Ingredient.objects.filter(pk=raw).update(name="Соль тест")
    basis = item()
    preparation = prep("Основа тест", [line(basis)])
    supplier = save_supplier(token(), {"name": "Поставка тест", "note": ""})
    for kind, key in [("ingredient", raw), ("preparation", preparation), ("supplier", supplier)]:
        assert transition(authenticated, kind, key, "trash")[0].status_code == 302
    response = authenticated.get(reverse("archive"), {"status": "trashed", "q": "ТЕСТ", "sort": "-name"})
    assert [obj.name for obj in response.context["page"]] == ["Соль тест", "Поставка тест", "Основа тест"]
    assert "Бессрочно" in response.content.decode()
    filtered = authenticated.get(reverse("archive"), {"status": "trashed", "kind": "supplier", "q": "тест"})
    assert [obj.pk for obj in filtered.context["page"]] == [supplier]
    assert raw not in [obj.pk for obj, _ in authenticated.get(reverse("ingredients")).context["rows"]]
    assert authenticated.get(reverse("ingredient-edit", args=[raw])).status_code == 302
    assert authenticated.get(reverse("supplier-edit", args=[supplier])).status_code == 302
    assert authenticated.get(reverse("preparation-edit", args=[preparation])).status_code == 302
    assert transition(authenticated, "ingredient", raw, "restore")[0].status_code == 302
    assert Ingredient.objects.get(pk=raw).status == "active"
    assert transition(authenticated, "supplier", supplier, "purge")[0].status_code == 302
    assert not Supplier.objects.filter(pk=supplier).exists()


def test_catalog_dependency_blocks_forged_post_and_forms_exclude_trash(authenticated, state):
    used, unused = item(), item()
    dish = save_dish(token(), {"name": "Зависимость", "base_price": D(500)}, [line(used)])
    transition_dish(token(), dish, "archive")
    url = reverse("catalog-transition", args=["ingredient", used, "trash"])
    response = authenticated.get(url)
    assert "Зависимость" in response.content.decode() and not response.context["can_apply"]
    before = facts()
    assert authenticated.post(url, {"token": response.context["form"]["token"].value()}).status_code == 400
    assert facts() == before
    transition_catalog(token(), "ingredient", unused, "trash")
    editor = authenticated.get(reverse("dish-create"))
    assert str(unused) not in str(editor.context["formset"][0]["component"])
    scenario = authenticated.get(reverse("scenario-editor"))
    assert str(unused) not in str(scenario.context["formset"][0]["ingredient"])
    with pytest.raises(ValidationError):
        save_dish(token(), {"name": "Недопустимо", "base_price": D(1)}, [line(unused)])


def test_json_http_download_confirm_replace_auth_and_replay(authenticated, user, state):
    key = item()
    original = authenticated.get(reverse("export-json"))
    assert original.status_code == 200 and original["Content-Type"] == "application/json"
    document = original.json()
    assert not ({"auth", "users", "sessions", "operations"} & document["data"].keys())
    update_ingredient(token(), key, {"name": "После копии", "loss": D(0)})
    old_token = token()
    response = upload(authenticated, document)
    assert response.status_code == 302
    session = RestoreSession.objects.get()
    review_url = reverse("restore-review", args=[session.pk])
    assert authenticated.get(review_url).status_code == 200
    body = confirmation(authenticated, session)
    before = facts()
    assert authenticated.post(review_url, body).status_code == 400 and facts() == before
    downloaded = authenticated.post(reverse("restore-download", args=[session.pk]))
    assert downloaded.status_code == 200 and downloaded.json()["data"]["ingredients"][0]["name"] == "После копии"
    assert authenticated.post(review_url, {"review_token": body["review_token"]}).status_code == 400 and facts() == before
    assert authenticated.post(review_url, body).status_code == 302
    assert Ingredient.objects.get(pk=key).name == document["data"]["ingredients"][0]["name"]
    assert authenticated.get(reverse("dashboard")).status_code == 200
    assert get_user_model().objects.filter(pk=user.pk).exists()
    assert AuditEntry.objects.filter(source="import").count() == 1
    before = facts()
    assert authenticated.post(review_url, body).status_code == 302 and facts() == before
    with pytest.raises(ValidationError):
        update_ingredient(old_token, key, {"name": "Старая форма", "loss": D(0)})
    assert "Данные восстановлены" in authenticated.get(review_url).content.decode()


def test_stale_restore_needs_new_download_and_checked_confirmation(authenticated, state):
    key = item()
    document = backups.export_snapshot()
    upload(authenticated, document)
    session = RestoreSession.objects.get()
    download_url, review_url = reverse("restore-download", args=[session.pk]), reverse("restore-review", args=[session.pk])
    old_body = confirmation(authenticated, session)
    assert authenticated.post(download_url).status_code == 200
    update_ingredient(token(), key, {"name": "Другая вкладка", "loss": D(0)})
    before = facts()
    assert authenticated.post(review_url, old_body).status_code == 400 and facts() == before
    assert authenticated.post(reverse("restore-refresh", args=[session.pk])).status_code == 302
    new_body = confirmation(authenticated, session)
    assert authenticated.post(review_url, new_body).status_code == 400 and facts() == before
    assert authenticated.post(download_url).json()["data"]["ingredients"][0]["name"] == "Другая вкладка"
    assert authenticated.post(review_url, old_body).status_code == 400 and facts() == before
    assert authenticated.post(review_url, {"review_token": new_body["review_token"]}).status_code == 400
    assert authenticated.post(review_url, new_body).status_code == 302


def test_bad_upload_and_prebackup_failure_do_not_change_domain(authenticated, state):
    item()
    before = facts()
    invalid = SimpleUploadedFile("bad.json", b'{"schema_version":999}', "application/json")
    assert authenticated.post(reverse("exchange"), {"backup_file": invalid}).status_code == 400
    assert facts() == before and not RestoreSession.objects.exists()
    document = backups.export_snapshot()
    with patch("tseh.services.backups._export_locked", side_effect=ValidationError("Не удалось сформировать копию")):
        assert upload(authenticated, document).status_code == 400
    assert facts() == before and not RestoreSession.objects.exists()


def test_access_csrf_restore_owner_and_download_methods(authenticated, user, state):
    anonymous = Client()
    for name in ["archive", "exchange", "export-json", "export-xlsx"]:
        assert anonymous.get(reverse(name)).status_code == 302
    item()
    upload(authenticated, backups.export_snapshot())
    session = RestoreSession.objects.get()
    for name in ["restore-download", "restore-refresh"]:
        assert authenticated.get(reverse(name, args=[session.pk])).status_code == 405
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    assert strict.post(reverse("restore-review", args=[session.pk]), {"confirm_download": "on"}).status_code == 403
    for name in ["restore-download", "restore-refresh"]:
        assert strict.post(reverse(name, args=[session.pk])).status_code == 403
    assert strict.post(reverse("exchange"), {}).status_code == 403
    other = get_user_model().objects.create_user(username="other", password="long-test-password")
    from tseh.models import AccountAccess
    AccountAccess.objects.create(user=other, is_owner=True)
    authenticated.force_login(other)
    assert authenticated.get(reverse("restore-review", args=[session.pk])).status_code == 400
    assert authenticated.post(reverse("restore-download", args=[session.pk])).status_code == 400


def test_xlsx_http_is_download_readonly_and_uses_analysis_discount(authenticated, state):
    from io import BytesIO
    from openpyxl import load_workbook
    key = item()
    save_dish(token(), {"name": "Отчёт", "base_price": D(1000)}, [line(key, "0.1")])
    authenticated.get(reverse("dashboard"), {"discount": "10"})
    before = facts()
    response = authenticated.get(reverse("export-xlsx"))
    assert response.status_code == 200 and response.content[:2] == b"PK" and facts() == before
    book = load_workbook(BytesIO(response.content), data_only=True)
    assert len(book.sheetnames) == 15 and "Блюда" in book.sheetnames
    sheet = book["Блюда"]
    headers = {cell.value: cell.column for cell in sheet[5]}
    assert sheet.cell(6, headers["Цена со скидкой, ₸"]).value == 900
    assert sheet.cell(6, headers["Себестоимость порции, ₸"]).value == 100


def test_excel_numeric_limit_is_explained_without_500_or_writes(authenticated, state):
    before = facts()
    with patch("tseh.transfer_views.build_xlsx", side_effect=ValidationError("Значение вне диапазона Excel. Используйте JSON.")):
        response = authenticated.get(reverse("export-xlsx"))
    assert response.status_code == 400 and "Используйте JSON" in response.content.decode()
    assert facts() == before
