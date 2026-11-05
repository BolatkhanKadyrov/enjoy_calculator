from datetime import date
from decimal import Decimal as D
from html.parser import HTMLParser

import pytest

from tseh.forms import ComponentSelect, LineForm, LineFormSet
from tseh.models import DomainState, Ingredient, Preparation
from tseh.services.commands import create_ingredient, save_preparation, stamp

pytestmark = pytest.mark.django_db


class Options(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.options = {}
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag == "option":
            values = dict(attrs)
            self.options[values["value"]] = values


def token():
    return stamp(DomainState.objects.get(pk=1))


@pytest.fixture
def catalog(state):
    expected = {}
    for unit, suggested in [("kg", "g"), ("l", "ml"), ("pc", "pc")]:
        for kind in ("ingredient", "packaging"):
            key = create_ingredient(token(), {"name": f"{kind} {unit}", "kind": kind, "unit": unit,
                "loss": D(0), "pack_price": D(1), "pack_size": D(1), "price_date": date(2026, 11, 4)})
            expected[f"ingredient:{key}"] = suggested
            if kind == "ingredient":
                prep = save_preparation(token(), {"name": f"Prep {unit}", "output_quantity": D(1), "output_unit": unit},
                    [{"ingredient_id": key, "quantity": D(1), "unit": unit}])
                expected[f"preparation:{prep}"] = suggested
    return expected


def test_metadata_for_each_dimension_and_component_kind(catalog):
    options = Options(str(LineForm()["component"])).options
    assert "data-unit" not in options.pop("")
    assert {key: option["data-unit"] for key, option in options.items()} == catalog


def test_empty_form_template_has_same_metadata_without_selected_component(catalog):
    empty = LineFormSet().empty_form
    assert empty["component"].auto_id == "id_form-__prefix__-component"
    options = Options(str(empty["component"])).options
    assert "data-unit" not in options[""]
    assert {key: value["data-unit"] for key, value in options.items() if key} == catalog
    assert all("selected" not in option for key, option in options.items() if key)


def test_preparation_template_excludes_packaging_metadata(catalog):
    empty = LineFormSet(form_kwargs={"preparation_recipe": True}).empty_form
    options = Options(str(empty["component"])).options
    forbidden = {f"ingredient:{key}" for key in Ingredient.objects.filter(kind="packaging").values_list("pk", flat=True)}
    assert not forbidden.intersection(options)
    assert len(options) == 7  # Three raw items, three preparations, and the placeholder.


def test_option_metadata_and_labels_are_escaped():
    widget = ComponentSelect(choices=[("", "Выберите"), ('unsafe"value', '<img src=x onerror="bad">')])
    widget.component_units = {'unsafe"value': 'g" onmouseover="bad'}
    rendered = widget.render("component", None)
    assert '<img' not in rendered
    options = Options(rendered).options
    assert options['unsafe"value']["data-unit"] == 'g" onmouseover="bad'
    assert "onmouseover" not in options['unsafe"value']


@pytest.mark.parametrize("prefix", ["ingredient", "preparation"])
def test_server_rejects_dimension_mismatch_despite_forged_metadata(catalog, prefix):
    key = next(key for key, hint in catalog.items() if key.startswith(prefix + ":") and hint == "g")
    form = LineForm({"component": key, "quantity": "1", "unit": "ml", "quantity_unit": "ml",
                     "data-unit": "ml", "component_unit": "l"})
    assert not form.is_valid()
    assert "несовместима" in str(form.non_field_errors())


def test_model_sort_order_remains_within_groups(catalog):
    assert Ingredient._meta.ordering == Preparation._meta.ordering == ["name", "id"]
    groups = dict(list(LineForm().fields["component"].choices)[1:])
    for label, queryset, prefix in [
        ("Сырьё", Ingredient.objects.filter(kind="ingredient"), "ingredient"),
        ("Упаковка", Ingredient.objects.filter(kind="packaging"), "ingredient"),
        ("Заготовки", Preparation.objects.all(), "preparation"),
    ]:
        assert groups[label] == [(f"{prefix}:{obj.pk}", obj.name) for obj in queryset.order_by("name", "id")]
