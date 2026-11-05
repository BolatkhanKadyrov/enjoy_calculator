from decimal import Decimal as D
from unittest.mock import patch
import uuid

import pytest
from django.urls import reverse

from tseh.models import AuditEntry, Dish, Ingredient, Preparation, Price, PriceApplication, Supplier
from tests.test_web import get_token
from tests.test_stage2_services import item

pytestmark = pytest.mark.django_db


def item_data(client, **extra):
    return {"token": get_token(client, "ingredient-create"), "name": "Без поставщика", "kind": "ingredient",
            "unit": "kg", "loss": "0", "pack_size": "5", "pack_price": "10000", "price_date": "2026-11-04", **extra}


@pytest.mark.parametrize("kind", ["ingredient", "packaging"])
def test_first_item_without_supplier_saves_price_and_audit(authenticated, state, kind):
    data = item_data(authenticated, kind=kind, unit="pc" if kind == "packaging" else "kg")
    response = authenticated.post(reverse("ingredient-create"), data)
    assert response.status_code == 302
    obj = Ingredient.objects.get()
    assert not Supplier.objects.exists()
    assert obj.current_price.supplier_id is None
    assert obj.current_price.pack_price == D(10000)
    assert PriceApplication.objects.get().price_id == obj.current_price_id
    audit = AuditEntry.objects.get(object_type="Price")
    assert audit.after["supplier_id"] is None and audit.after["supplier_name"] is None
    for url in [response.url, reverse("ingredients"), reverse("journal")]:
        page = authenticated.get(url)
        assert page.status_code == 200
    assert "Не указан" in authenticated.get(response.url).content.decode()
    assert "Не указан" in authenticated.get(reverse("ingredients")).content.decode()
    assert authenticated.post(reverse("ingredient-create"), data).status_code == 302
    assert Ingredient.objects.count() == 1 and Price.objects.count() == 1


def test_optional_supplier_does_not_accept_nonexistent_reference(authenticated, state):
    response = authenticated.post(reverse("ingredient-create"), item_data(authenticated, supplier=str(uuid.uuid4())))
    assert response.status_code == 400 and "supplier" in response.context["form"].errors
    assert not Ingredient.objects.exists() and not Price.objects.exists()


def test_optional_supplier_audit_failure_is_atomic(authenticated, state):
    with patch("tseh.services.commands.audit", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError):
            authenticated.post(reverse("ingredient-create"), item_data(authenticated))
    assert not Ingredient.objects.exists() and not Price.objects.exists() and not PriceApplication.objects.exists()


def recipe_data(client, name, component, count=3):
    data = {"token": get_token(client, name), "name": "Проверка удаления", "base_price": "1000",
            "output_quantity": "1", "output_unit": "kg", "form-TOTAL_FORMS": str(count),
            "form-INITIAL_FORMS": "0", "form-MIN_NUM_FORMS": "1", "form-MAX_NUM_FORMS": "100", "intent": "preview"}
    for index in range(count):
        data.update({f"form-{index}-ingredient": str(component), f"form-{index}-quantity": str(index + 1), f"form-{index}-unit": "g"})
    return data


@pytest.mark.parametrize("page,model", [("dish-create", Dish), ("preparation-create", Preparation)])
def test_remove_middle_row_preview_save_without_javascript(authenticated, state, page, model):
    component = item(price="1000")
    data = recipe_data(authenticated, page, component)
    # The remove button must work even with an invalid amount in the removed row.
    data.update(remove_line="form-1", **{"form-1-quantity": "bad"})
    removed = authenticated.post(reverse(page), data)
    assert removed.status_code == 200 and "preview" not in removed.context
    assert not model.objects.exists()
    assert 'data-prefix="form-1" hidden' in removed.content.decode()
    assert removed.context["form"]["name"].value() == data["name"]
    next_data = removed.context["formset"].data.copy()
    next_data.pop("remove_line")
    preview = authenticated.post(reverse(page), next_data)
    assert preview.status_code == 200
    assert preview.context["preview"].total == D("4.00")  # 1 g + 3 g at 1000/kg
    assert not model.objects.exists()
    next_data.update(intent="save", preview_token=preview.context["preview_token"])
    assert authenticated.post(reverse(page), next_data).status_code == 302
    assert list(model.objects.get().lines.values_list("quantity", flat=True)) == [D(1), D(3)]
    assert Ingredient.objects.filter(pk=component).exists()


@pytest.mark.parametrize("page,model", [("dish-create", Dish), ("preparation-create", Preparation)])
def test_delete_after_preview_requires_new_confirmation(authenticated, state, page, model):
    component = item()
    data = recipe_data(authenticated, page, component)
    preview = authenticated.post(reverse(page), data)
    data.update(intent="save", preview_token=preview.context["preview_token"], **{"form-1-DELETE": "on"})
    response = authenticated.post(reverse(page), data)
    assert response.status_code == 200 and not model.objects.exists()
    assert response.context["preview"].total == D("4.00")
    assert 'type="checkbox"' not in response.content.decode()
    assert 'class="button remove-line"' in response.content.decode()


@pytest.mark.parametrize("page", ["dish-create", "preparation-create"])
def test_last_row_removal_is_explained_and_values_preserved(authenticated, state, page):
    data = recipe_data(authenticated, page, item(), count=1)
    data["form-0-DELETE"] = "on"
    response = authenticated.post(reverse(page), data)
    assert response.status_code == 400 and response.context["formset"].non_form_errors()
    assert response.context["form"]["name"].value() == data["name"]


def test_replace_deleted_row_at_limit_without_javascript(authenticated, state):
    data = recipe_data(authenticated, "dish-create", item(), count=100)
    data.update(add_line="1", **{"form-35-DELETE": "on"})
    response = authenticated.post(reverse("dish-create"), data)
    assert response.status_code == 200 and not Dish.objects.exists()
    bound = response.context["formset"].data
    assert bound["form-TOTAL_FORMS"] == "100"
    assert not bound.get("form-35-DELETE") and not bound.get("form-35-ingredient")
    assert bound["form-36-quantity"] == "37"
    data.pop("form-35-DELETE")
    limit = authenticated.post(reverse("dish-create"), data)
    assert "не более 100 компонентов" in limit.content.decode()


def test_remove_during_edit_does_not_write_before_confirmation(authenticated, state):
    from tseh.services.commands import save_dish
    from tests.test_stage2_services import token, line
    component = item()
    dish_id = save_dish(token(), {"name": "Исходное", "base_price": D(500)}, [line(component), line(component, "2")])
    url = reverse("dish-edit", kwargs={"pk": dish_id})
    form = authenticated.get(url)
    data = {"token": form.context["form"].initial["token"], "name": "Изменение", "base_price": "500", "form-TOTAL_FORMS": "2",
            "form-INITIAL_FORMS": "2", "form-0-ingredient": str(component), "form-0-quantity": "1", "form-0-unit": "kg", "remove_line": "form-1"}
    before_audit = AuditEntry.objects.count()
    removed = authenticated.post(url, data)
    assert removed.status_code == 200
    assert Dish.objects.get(pk=dish_id).name == "Исходное" and Dish.objects.get(pk=dish_id).lines.count() == 2
    assert AuditEntry.objects.count() == before_audit


@pytest.mark.parametrize("page", ["dish-create", "preparation-create"])
def test_implicit_submit_defaults_to_calculation_not_removal(authenticated, state, page):
    from html.parser import HTMLParser

    class RecipeButtons(HTMLParser):
        inside = False
        first_submit = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form":
                self.inside = attrs.get("id") == "recipe-form"
            if self.inside and tag == "button" and attrs.get("type", "submit") == "submit" and self.first_submit is None:
                self.first_submit = attrs

        def handle_endtag(self, tag):
            if tag == "form":
                self.inside = False

    component = item()
    html = RecipeButtons()
    html.feed(authenticated.get(reverse(page)).content.decode())
    assert (html.first_submit["name"], html.first_submit["value"]) == ("intent", "preview")
    data = recipe_data(authenticated, page, component)
    data[html.first_submit["name"]] = html.first_submit["value"]
    response = authenticated.post(reverse(page), data)
    assert response.context["preview"].total == D("6.00")
    assert len(response.context["preview_rows"]) == 3
