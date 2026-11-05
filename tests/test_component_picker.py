from datetime import date
from decimal import Decimal as D
import uuid

import pytest
from django.urls import reverse

from tseh.forms import LineForm
from tseh.models import Dish, DomainState, Ingredient, Preparation, Supplier
from tseh.services.commands import create_ingredient, save_preparation, stamp
from tests.test_web import get_token

pytestmark = pytest.mark.django_db


def token():
    return stamp(DomainState.objects.get(pk=1))


@pytest.fixture
def components(state):
    def create(name, kind, unit, price):
        pk = create_ingredient(token(), {
            "name": name, "kind": kind, "unit": unit, "loss": D(0),
            "pack_price": D(price), "pack_size": D(1), "price_date": date(2026, 11, 4),
        })
        return Ingredient.objects.get(pk=pk)
    raw = create("Сырьё", "ingredient", "kg", "1000")
    packaging = create("Контейнер", "packaging", "pc", "50")
    pk = save_preparation(token(), {"name": "Заготовка", "output_quantity": D(2), "output_unit": "kg"},
                          [{"ingredient_id": raw.pk, "quantity": D(1), "unit": "kg"}])
    return raw, packaging, Preparation.objects.get(pk=pk)


def choice(obj):
    return f"{'ingredient' if isinstance(obj, Ingredient) else 'preparation'}:{obj.pk}"


def form_data(obj, **changes):
    return {"component": choice(obj), "quantity": "1", "unit": obj.unit, **changes}


def test_grouped_choices_and_hidden_legacy_fields(components):
    raw, packaging, preparation = components
    form = LineForm(prefix="form-3")
    groups = dict(list(form.fields["component"].choices)[1:])
    assert groups == {
        "Сырьё": [(choice(raw), raw.name)], "Упаковка": [(choice(packaging), packaging.name)],
        "Заготовки": [(choice(preparation), preparation.name)],
    }
    assert form["component"].auto_id == "id_form-3-component"
    assert form.fields["ingredient"].widget.is_hidden
    assert form.fields["preparation"].widget.is_hidden
    for obj in components:
        bound = LineForm(form_data(obj))
        assert bound.is_valid(), bound.errors
        assert bound.cleaned_data["component"] == obj


def test_preparation_choices_exclude_packaging_and_reject_post(components):
    raw, packaging, preparation = components
    form = LineForm(preparation_recipe=True)
    groups = dict(list(form.fields["component"].choices)[1:])
    assert "Упаковка" not in groups
    assert groups["Сырьё"] == [(choice(raw), raw.name)]
    assert groups["Заготовки"] == [(choice(preparation), preparation.name)]
    rejected = LineForm(form_data(packaging, ingredient=str(raw.pk)), preparation_recipe=True)
    assert not rejected.is_valid() and "component" in rejected.errors
    assert rejected.cleaned_data["ingredient"] is None


@pytest.mark.parametrize("selected", [0, 1, 2])
def test_explicit_component_ignores_both_legacy_values(components, selected):
    obj = components[selected]
    form = LineForm(form_data(obj, ingredient="invalid-old-id", preparation=str(uuid.uuid4())))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["ingredient"] == (obj if selected < 2 else None)
    assert form.cleaned_data["preparation"] == (obj if selected == 2 else None)


@pytest.mark.parametrize("invalid", ["", "invalid", "ingredient:00000000-0000-0000-0000-000000000001",
                                     "preparation:00000000-0000-0000-0000-000000000001", "supplier:anything"])
def test_explicit_invalid_component_never_falls_back(components, invalid):
    raw = components[0]
    form = LineForm(form_data(raw, component=invalid, ingredient=str(raw.pk)))
    assert not form.is_valid() and "component" in form.errors
    assert form.cleaned_data["ingredient"] is None
    assert form.cleaned_data["preparation"] is None
    assert form["component"].value() == invalid


def test_stale_component_removed_from_catalog_rejected(components):
    raw, _, preparation = components
    selected = choice(preparation)
    preparation.lines.all().delete()
    preparation.delete()
    form = LineForm(form_data(raw, component=selected, ingredient=str(raw.pk)))
    assert not form.is_valid() and "component" in form.errors


@pytest.mark.parametrize("selected", [0, 1, 2])
def test_legacy_post_and_get_initial_keep_selected_component(components, selected):
    obj = components[selected]
    kind = "ingredient" if selected < 2 else "preparation"
    initial = {kind: obj.pk, "quantity": D(1), "unit": obj.unit}
    assert LineForm(initial=initial)["component"].value() == choice(obj)
    form = LineForm({kind: str(obj.pk), "quantity": "1", "unit": obj.unit})
    assert form.is_valid(), form.errors
    assert form.cleaned_data[kind] == obj
    assert form["component"].value() == choice(obj)


def test_invalid_quantity_preserves_component_selection(components):
    obj = components[2]
    form = LineForm(form_data(obj, quantity="bad"))
    assert not form.is_valid() and "quantity" in form.errors
    assert form["component"].value() == choice(obj)


@pytest.mark.parametrize("page,model", [("dish-create", Dish), ("preparation-create", Preparation)])
def test_new_component_preview_save_reopen_without_supplier(authenticated, components, page, model):
    raw, packaging, preparation = components
    selected = [raw, preparation] + ([packaging] if model is Dish else [])
    data = {"token": get_token(authenticated, page), "name": "Единый выбор", "base_price": "2000",
            "output_quantity": "1", "output_unit": "kg", "form-TOTAL_FORMS": str(len(selected)),
            "form-INITIAL_FORMS": "0", "intent": "preview"}
    for index, obj in enumerate(selected):
        data.update({f"form-{index}-component": choice(obj), f"form-{index}-quantity": "1", f"form-{index}-unit": obj.unit})
    preview = authenticated.post(reverse(page), data)
    assert preview.status_code == 200
    assert preview.context["preview"].total == D("1550" if model is Dish else "1500")
    data.update(intent="save", preview_token=preview.context["preview_token"])
    saved = authenticated.post(reverse(page), data)
    assert saved.status_code == 302
    obj = model.objects.get(name="Единый выбор")
    edit_page = "dish-edit" if model is Dish else "preparation-edit"
    reopened = authenticated.get(reverse(edit_page, kwargs={"pk": obj.pk}))
    assert reopened.status_code == 200
    assert [form["component"].value() for form in reopened.context["formset"]] == [choice(row) for row in selected]
    assert not Supplier.objects.exists()
    assert raw.current_price.supplier_id is None


def test_new_choice_preserves_exact_quantity_conversion(components):
    raw = components[0]
    form = LineForm(form_data(raw, quantity="0.004000000001", unit="g", quantity_unit="kg"))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["quantity"] == D("4.000000001")
    assert form.cleaned_data["quantity_unit"] == "g"
