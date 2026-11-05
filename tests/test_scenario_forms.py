from decimal import Decimal as D

import pytest

from tseh.scenario_forms import ScenarioFormSet
from tseh.services.prices import create_price
from tests.test_stage2_services import item, token

pytestmark = pytest.mark.django_db


def form_data(key, **changes):
    return {"rows-TOTAL_FORMS": "1", "rows-INITIAL_FORMS": "0", "rows-MIN_NUM_FORMS": "1",
        "rows-MAX_NUM_FORMS": "100", "rows-0-ingredient": str(key), "rows-0-mode": "new",
        "rows-0-saved_price": "", "rows-0-new-supplier": "", "rows-0-new-price_date": "2026-11-04",
        "rows-0-new-pack_size": "1", "rows-0-new-pack_price": "1000", "rows-0-new-comment": "", **changes}


def test_exact_decimal_comma_empty_and_explicit_zero(state):
    key = item()
    forms = ScenarioFormSet(form_data(key, **{"rows-0-new-pack_size": "0,000000000001",
        "rows-0-new-pack_price": "0,01"}), prefix="rows")
    assert forms.is_valid(), forms.errors
    row = forms[0].row_data()
    assert row["data"]["pack_size"] == D("0.000000000001") and row["data"]["pack_price"] == D("0.01")
    for changes in ({"rows-0-new-pack_price": ""}, {"rows-0-new-pack_price": "1,001"},
                    {"rows-0-new-pack_price": "0"}, {"rows-0-ingredient": "bad-uuid"},
                    {"rows-0-new-supplier": "bad-uuid"}):
        assert not ScenarioFormSet(form_data(key, **changes), prefix="rows").is_valid()
    assert ScenarioFormSet(form_data(key, **{"rows-0-new-pack_price": "0",
        "rows-0-new-confirm_zero": "on"}), prefix="rows").is_valid()


def test_existing_mode_ignores_new_fields_but_rejects_foreign_saved_price(state):
    a, b = item(), item()
    create_price(token(), a, {"pack_size": D(1), "pack_price": D(2000), "price_date": "2026-11-04",
        "supplier_id": None, "comment": "", "is_one_off": False})
    from tseh.models import Ingredient, Price
    offer = Price.objects.filter(ingredient_id=a).exclude(pk=Ingredient.objects.get(pk=a).current_price_id).get()
    data = form_data(a, **{"rows-0-mode": "existing", "rows-0-saved_price": str(offer.pk),
        "rows-0-new-pack_price": "", "rows-0-new-pack_size": ""})
    forms = ScenarioFormSet(data, prefix="rows")
    assert forms.is_valid(), forms.errors
    assert forms[0].row_data() == {"ingredient_id": a, "price_id": offer.pk}
    assert not forms[0].new_price_form.errors
    assert not ScenarioFormSet({**data, "rows-0-ingredient": str(b)}, prefix="rows").is_valid()
    assert not ScenarioFormSet({**data, "rows-0-saved_price": "bad"}, prefix="rows").is_valid()


def test_new_mode_preserves_but_ignores_inactive_saved_price(state):
    key = item()
    for inactive in ("old-price-from-another-position", ""):
        forms = ScenarioFormSet(form_data(key, **{"rows-0-saved_price": inactive}), prefix="rows")
        assert forms.is_valid(), forms.errors
        assert forms[0]["saved_price"].value() == inactive
        assert forms[0].row_data()["data"]["pack_price"] == D(1000)
        assert "price_id" not in forms[0].row_data()


def test_duplicate_and_empty_scenarios_rejected_and_choice_queries_bounded(state, django_assert_num_queries):
    key = item()
    data = form_data(key)
    data["rows-TOTAL_FORMS"] = "2"
    for name, value in list(data.items()):
        if name.startswith("rows-0-"):
            data[name.replace("rows-0-", "rows-1-")] = value
    with django_assert_num_queries(3):
        forms = ScenarioFormSet(data, prefix="rows")
        assert not forms.is_valid()
        assert "одну строку" in str(forms.non_form_errors())
        for form in forms:
            str(form["ingredient"])
            str(form["saved_price"])
            str(form.new_price_form["supplier"])
    assert not ScenarioFormSet({**data, "rows-TOTAL_FORMS": "0"}, prefix="rows").is_valid()
    assert not ScenarioFormSet({**data, "rows-TOTAL_FORMS": "101"}, prefix="rows").is_valid()
