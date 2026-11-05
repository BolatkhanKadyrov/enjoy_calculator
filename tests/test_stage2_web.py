from decimal import Decimal as D

import pytest
from django.urls import reverse

from tseh.models import AuditEntry, Dish, DomainState, Preparation
from tseh.services.commands import save_dish, save_preparation, save_thresholds
from tests.test_stage2_services import item, line, prep, token

pytestmark = pytest.mark.django_db


def edit_data(client, preparation, quantity="2", unit="kg", component=None):
    response = client.get(reverse("preparation-edit", kwargs={"pk": preparation.pk}))
    return {"token": response.context["form"].initial["token"], "name": preparation.name,
        "output_quantity": quantity, "output_unit": unit, "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "1",
        "form-MIN_NUM_FORMS": "1", "form-MAX_NUM_FORMS": "100", "form-0-ingredient": str(component),
        "form-0-quantity": "1", "form-0-unit": "kg", "intent": "preview"}


def test_preparation_ui_preview_transitive_effect_and_apply(authenticated, state):
    x = item(price="3000")
    a = prep("A", [line(x)], output="2")
    b = prep("B", [line(a, "0.5", prep=True)], output="0.5")
    dish = save_dish(token(), {"name": "Блюдо", "base_price": D(500)}, [line(b, "100", "g", True)])
    obj = Preparation.objects.get(pk=a)
    data = edit_data(authenticated, obj, quantity="1,5", component=x)
    response = authenticated.post(reverse("preparation-edit", kwargs={"pk": a}), data)
    assert response.status_code == 200
    assert len(response.context["prep_impacts"]) == 2
    impact = response.context["dish_impacts"][0]
    assert impact[0].pk == dish and impact[1].total == D("150.00") and impact[2].total == D("200.00")
    assert Preparation.objects.get(pk=a).output_quantity == 2
    data.update(intent="save", preview_token=response.context["preview_token"])
    response = authenticated.post(reverse("preparation-edit", kwargs={"pk": a}), data)
    assert response.status_code == 302
    assert Preparation.objects.get(pk=a).output_quantity == D("1.5")
    assert authenticated.get(response.url).status_code == 200
    assert authenticated.get(reverse("preparations")).status_code == 200
    assert authenticated.get(reverse("ingredient-detail", kwargs={"pk": x})).status_code == 200


def test_new_preparation_created_through_ui_and_not_before_confirmation(authenticated, state):
    x = item()
    url = reverse("preparation-create")
    get = authenticated.get(url)
    data = {"token": get.context["form"].initial["token"], "name": "Новая", "output_quantity": "500", "output_unit": "g",
        "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "0", "form-MIN_NUM_FORMS": "1", "form-MAX_NUM_FORMS": "100",
        "form-0-ingredient": str(x), "form-0-quantity": "1", "form-0-unit": "kg", "intent": "preview"}
    response = authenticated.post(url, data)
    assert response.status_code == 200 and not Preparation.objects.exists()
    assert response.context["preview_unit_price"] == 2000
    data.update(intent="save", preview_token=response.context["preview_token"])
    assert authenticated.post(url, data).status_code == 302
    assert authenticated.post(url, data).status_code == 302
    assert Preparation.objects.count() == 1


def test_unused_dimension_change_shows_both_units(authenticated, state):
    x = item(price="3000")
    a = prep("A", [line(x)], output="2")
    data = edit_data(authenticated, Preparation.objects.get(pk=a), unit="l", component=x)
    response = authenticated.post(reverse("preparation-edit", kwargs={"pk": a}), data)
    assert response.status_code == 200
    assert response.context["prep_impacts"][0][3:] == ("kg", "l")
    text = response.content.decode()
    assert "₸/кг" in text and "₸/л" in text


def test_discount_analysis_preserves_base_prices_and_journal(authenticated, state):
    x = item(price="700")
    dish_id = save_dish(token(), {"name": "Блюдо", "base_price": D(1400)}, [line(x)])
    before = AuditEntry.objects.count()
    response = authenticated.get(reverse("dashboard"), {"discount": "10"})
    assert response.status_code == 200
    assessment = response.context["rows"][0][2]
    assert assessment.effective == 1260
    assert "~" in response.content.decode() and "55,56" in response.content.decode()
    assert Dish.objects.get(pk=dish_id).base_price == 1400
    assert AuditEntry.objects.count() == before
    detail = authenticated.get(reverse("dish-detail", kwargs={"pk": dish_id}))
    assert detail.context["assessment"].effective == 1260
    invalid = authenticated.get(reverse("dashboard"), {"discount": "100"})
    assert invalid.context["discount_form"].errors
    assert authenticated.session["analysis_discount"] == "10"


def test_threshold_conflicts_warning_and_no_false_save_message(authenticated, state):
    x = item()
    save_thresholds(token(), {"green": D(40), "orange": D(60)})
    dish_id = save_dish(token(), {"name": "Конфликтное блюдо", "base_price": D(1000), "green_override": D(55)}, [line(x, "0.4")])
    url = reverse("thresholds")
    form = authenticated.get(url)
    response = authenticated.post(url, {"token": form.context["form"].initial["token"], "green": "40", "orange": "50"})
    assert response.status_code == 302
    page = authenticated.get(url)
    assert [dish.pk for dish in page.context["conflicts"]] == [dish_id]
    detail = authenticated.get(reverse("dish-detail", kwargs={"pk": dish_id}))
    assert detail.context["assessment"].zone == "invalid" and detail.context["assessment"].minimum is None
    response = authenticated.post(url, {"token": page.context["form"].initial["token"], "green": "60", "orange": "50"})
    assert response.status_code == 400
    assert "изменение сохранено" not in response.content.decode().lower()
    assert "пороги сохранены" not in response.content.decode().lower()


def test_high_threshold_warning_and_memo_present(authenticated, state):
    save_thresholds(token(), {"green": D(90), "orange": D(120)})
    response = authenticated.get(reverse("thresholds"))
    text = response.content.decode()
    assert "Порог выше 100%" in text
    assert "цвет сам по себе не подтверждает прибыль или убыток" in text


def test_dish_form_accepts_preparation_component(authenticated, state):
    x = item()
    a = prep("A", [line(x)], output="2")
    url = reverse("dish-create")
    page = authenticated.get(url)
    data = {"token": page.context["form"].initial["token"], "name": "Из заготовки", "base_price": "250",
        "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "0", "form-MIN_NUM_FORMS": "1", "form-MAX_NUM_FORMS": "100",
        "form-0-preparation": str(a), "form-0-quantity": "100", "form-0-unit": "g", "intent": "preview"}
    response = authenticated.post(url, data)
    assert response.status_code == 200
    assert response.context["preview"].total == D("50.00")
    data.update(intent="save", preview_token=response.context["preview_token"])
    saved = authenticated.post(url, data)
    assert saved.status_code == 302
    assert "Уже учтены" in authenticated.get(saved.url).content.decode()


def test_output_unit_change_without_js_converts_then_saves_once(authenticated, state):
    x = item(price="3000")
    a = prep("A", [line(x)], output="2")
    url = reverse("preparation-edit", kwargs={"pk": a})
    data = edit_data(authenticated, Preparation.objects.get(pk=a), quantity="2", unit="g", component=x)
    data["output_input_unit"] = "kg"
    preview = authenticated.post(url, data)
    assert preview.status_code == 200
    assert preview.context["form"].cleaned_data["output_quantity"] == 2000
    assert preview.context["preview_unit_price"] == 1500
    assert preview.context["form"]["output_quantity"].value() == "2000"
    assert preview.context["form"]["output_input_unit"].value() == "g"
    data.update(output_quantity="2000", output_input_unit="g", intent="save", preview_token=preview.context["preview_token"])
    assert authenticated.post(url, data).status_code == 302
    assert Preparation.objects.get(pk=a).output_quantity == 2000


@pytest.mark.parametrize("amount,source,target,expected", [(".5", "kg", "g", "500"), ("0.000000000001", "g", "kg", "0.000000000000001"), ("999999999999.123456789012", "kg", "g", "999999999999123.456789012")])
def test_recipe_unit_conversion_preserves_valid_extremes(state, amount, source, target, expected):
    from tseh.forms import LineForm
    x = item()
    form = LineForm({"ingredient": str(x), "quantity": amount, "quantity_unit": source, "unit": target})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["quantity"] == D(expected)
    assert form["quantity_unit"].value() == target
