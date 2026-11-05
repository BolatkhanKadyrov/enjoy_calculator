from decimal import Decimal as D
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from tseh.models import AuditEntry, Dish, DomainState, Ingredient, Price, PriceApplication, Supplier
from tseh.services.commands import save_supplier, stamp

pytestmark = pytest.mark.django_db


def get_token(client, name, **kwargs):
    response = client.get(reverse(name, kwargs=kwargs or None))
    assert response.status_code == 200
    return response.context["form"].initial["token"]


def create_item(client, amount="10000", loss="25", name="Курица"):
    token = get_token(client, "supplier-create")
    assert client.post(reverse("supplier-create"), {"token": token, "name": "Поставщик", "note": ""}).status_code == 302
    supplier = Supplier.objects.latest("id")
    token = get_token(client, "ingredient-create")
    response = client.post(reverse("ingredient-create"), {"token": token, "name": name, "unit": "kg", "loss": loss,
        "supplier": supplier.pk, "pack_size": "5", "pack_price": amount, "price_date": "2026-11-04", "confirm_zero": "on"})
    assert response.status_code == 302
    return Ingredient.objects.get(name=name)


def recipe(client, item, **extra):
    return {"token": get_token(client, "dish-create"), "name": "Курица в соусе", "base_price": "700,50",
        "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "0", "form-MIN_NUM_FORMS": "1", "form-MAX_NUM_FORMS": "100",
        "form-0-ingredient": str(item.pk), "form-0-quantity": "100", "form-0-unit": "g", "intent": "preview", **extra}


def test_full_ui_path_preview_save_reopen(authenticated, state):
    client = authenticated
    item = create_item(client)
    data = recipe(client, item)
    response = client.post(reverse("dish-create"), data)
    assert response.status_code == 200
    assert response.context["preview"].total == D("266.67")
    assert not Dish.objects.exists()
    data.update(intent="save", preview_token=response.context["preview_token"])
    response = client.post(reverse("dish-create"), data)
    assert response.status_code == 302
    detail = client.get(response.url)
    assert detail.status_code == 200
    assert detail.context["result"].total == D("266.67")
    assert Dish.objects.get().base_price == D("700.50")
    assert "266,67" in detail.content.decode()
    repeat = client.post(reverse("dish-create"), data)
    assert repeat.status_code == 302
    assert Dish.objects.count() == 1
    assert AuditEntry.objects.filter(object_type="Dish").count() == 1
    for name in ["dashboard", "ingredients", "suppliers", "journal"]:
        assert client.get(reverse(name)).status_code == 200


def test_save_requires_matching_preview(authenticated, state):
    item = create_item(authenticated)
    data = recipe(authenticated, item, intent="save")
    response = authenticated.post(reverse("dish-create"), data)
    assert response.status_code == 200
    assert not Dish.objects.exists()
    data.update(preview_token=response.context["preview_token"], **{"form-0-quantity": "200"})
    response = authenticated.post(reverse("dish-create"), data)
    assert response.status_code == 200
    assert response.context["preview"].total == D("533.33")
    assert not Dish.objects.exists()


def test_stale_preview_and_two_tabs(authenticated, state):
    item = create_item(authenticated)
    data = recipe(authenticated, item)
    preview = authenticated.post(reverse("dish-create"), data)
    token = get_token(authenticated, "ingredient-edit", pk=item.pk)
    assert authenticated.post(reverse("ingredient-edit", kwargs={"pk": item.pk}), {"token": token, "name": item.name, "loss": "10"}).status_code == 302
    data.update(intent="save", preview_token=preview.context["preview_token"])
    assert authenticated.post(reverse("dish-create"), data).status_code == 400
    assert not Dish.objects.exists()


@pytest.mark.parametrize("name", ["dashboard", "ingredients", "ingredient-create", "dish-create", "suppliers", "journal"])
def test_anonymous_cannot_read_domain(client, name):
    response = client.get(reverse(name))
    assert response.status_code == 302
    assert response.url.startswith("/login/")


def test_two_independent_sessions_allowed(user, state):
    first, second = Client(), Client()
    assert first.login(username="test-team", password="test-only-strong-password")
    assert second.login(username="test-team", password="test-only-strong-password")
    assert first.get("/").status_code == second.get("/").status_code == 200


def test_csrf_and_post_only_logout(user, state):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert client.post(reverse("supplier-create"), {"name": "X"}).status_code == 403
    assert client.get(reverse("logout")).status_code == 405


def test_bad_signature_does_not_save(authenticated, state):
    response = authenticated.post(reverse("supplier-create"), {"token": "tampered", "name": "X", "note": ""})
    assert response.status_code == 400
    assert not Supplier.objects.exists()


@pytest.mark.parametrize("changes", [{"pack_size": "0"}, {"loss": "100"}, {"pack_price": ""}, {"pack_price": "-1"}, {"pack_price": "1.005"}, {"pack_price": "0", "confirm_zero": ""}])
def test_invalid_price_input_no_partial_save(authenticated, state, changes):
    supplier = save_supplier(stamp(state), {"name": "Поставщик", "note": ""})
    data = {"token": get_token(authenticated, "ingredient-create"), "name": "Курица", "unit": "kg", "loss": "25",
        "supplier": supplier, "pack_size": "5", "pack_price": "10000", "price_date": "2026-11-04", **changes}
    response = authenticated.post(reverse("ingredient-create"), data)
    assert response.status_code == 400
    assert not Ingredient.objects.exists()
    assert not Price.objects.exists()
    assert not PriceApplication.objects.exists()


def test_incompatible_units_rejected_in_ui(authenticated, state):
    item = create_item(authenticated)
    response = authenticated.post(reverse("dish-create"), recipe(authenticated, item, **{"form-0-unit": "l"}))
    assert response.status_code == 400
    assert not Dish.objects.exists()


def test_journal_is_read_only_and_escapes_text(authenticated, state):
    save_supplier(stamp(state), {"name": "<script>alert(1)</script>", "note": "Контакт"})
    response = authenticated.get(reverse("journal"))
    assert "&lt;script&gt;" in response.content.decode()
    assert "<script>alert(1)</script>" not in response.content.decode()
    assert authenticated.post(reverse("journal"), {}).status_code == 405


def test_empty_recipe_rejected(authenticated, state):
    item = create_item(authenticated)
    response = authenticated.post(reverse("dish-create"), recipe(authenticated, item, **{"form-0-DELETE": "on"}))
    assert response.status_code == 400
    assert not Dish.objects.exists()


def test_preview_difference_preserves_large_valid_amounts(authenticated, state):
    from tseh.services.commands import create_ingredient, save_dish
    from datetime import date
    from fractions import Fraction
    supplier = save_supplier(stamp(state), {"name": "Поставщик", "note": ""})
    state.refresh_from_db()
    item_id = create_ingredient(stamp(state), {"name": "Микрофасовка", "unit": "kg", "loss": D(0),
        "pack_price": D("999999999999.99"), "pack_size": D("0.000000000003"), "supplier_id": supplier,
        "price_date": date(2026, 11, 4)})
    state.refresh_from_db()
    dish_id = save_dish(stamp(state), {"name": "Граница", "base_price": D(1)}, [{"ingredient_id": item_id, "quantity": D(1), "unit": "kg"}])
    url = reverse("dish-edit", kwargs={"pk": dish_id})
    token = get_token(authenticated, "dish-edit", pk=dish_id)
    response = authenticated.post(url, {"token": token, "name": "Граница", "base_price": "1",
        "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "1", "form-MIN_NUM_FORMS": "1", "form-MAX_NUM_FORMS": "100",
        "form-0-ingredient": str(item_id), "form-0-quantity": "100000000000.000000000001", "form-0-unit": "kg", "intent": "preview"})
    assert response.status_code == 200
    assert response.context["difference"] == Fraction(D("33333333332999666666667003333333333.33"))


def test_unchanged_edit_preserves_quantity_through_browser_forms(authenticated, state):
    item = create_item(authenticated, loss="0")
    data = recipe(authenticated, item, **{"form-0-quantity": "0.004000000001", "form-0-unit": "kg"})
    preview = authenticated.post(reverse("dish-create"), data)
    data.update(intent="save", preview_token=preview.context["preview_token"])
    response = authenticated.post(reverse("dish-create"), data)
    dish = Dish.objects.get()
    detail = authenticated.get(response.url)
    assert "4,000000001 г" in detail.content.decode()
    edit_url = reverse("dish-edit", kwargs={"pk": dish.pk})
    edit = authenticated.get(edit_url)
    data.update(token=edit.context["form"].initial["token"], intent="preview", **{"form-INITIAL_FORMS": "1"})
    preview = authenticated.post(edit_url, data)
    data.update(intent="save", preview_token=preview.context["preview_token"])
    assert authenticated.post(edit_url, data).status_code == 302
    assert dish.lines.get().quantity == D("0.004000000001")
