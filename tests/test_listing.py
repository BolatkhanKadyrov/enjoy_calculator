from datetime import datetime
from decimal import Decimal as D
from urllib.parse import parse_qs
import uuid

import pytest
from django.urls import reverse

from tseh.models import AuditEntry, DomainState, Ingredient, Supplier
from tseh.services.commands import create_ingredient, save_dish, save_preparation, save_supplier
from tests.test_stage2_services import token, line

pytestmark = pytest.mark.django_db


def raw(name, price="1000", size="1", supplier=None, unit="kg", kind="ingredient", loss="0"):
    return create_ingredient(token(), {"name": name, "kind": kind, "unit": unit, "loss": D(loss),
        "pack_price": D(price), "pack_size": D(size), "supplier_id": supplier, "price_date": "2026-11-04"},
        confirm_zero=D(price) == 0)


def ids(response, name="rows"):
    return [row[0].pk for row in response.context[name]]


def test_cyrillic_combined_filters_and_numeric_unit_price(authenticated, state):
    supplier = save_supplier(token(), {"name": "ТОО", "note": ""})
    a = raw("МУКА А", "10000", "100", supplier, loss="25")  # 100/kg
    b = raw("Мука Б", "2000", "1", supplier, loss="5")      # 2000/kg
    raw("Мука В", supplier=None)
    raw("Мука упаковка", kind="packaging")
    url = reverse("ingredients")
    query = {"q": "мука", "kind": "ingredient", "unit": "kg", "supplier": str(supplier), "sort": "price"}
    response = authenticated.get(url, query)
    assert ids(response) == [a, b]
    assert response.context["list_active"] and response.context["list_total"] == 2
    assert parse_qs(response.context["list_query"])["supplier"] == [str(supplier)]
    assert ids(authenticated.get(url, {**query, "sort": "-loss"})) == [a, b]
    assert ids(authenticated.get(url, {**query, "sort": "-price"})) == [b, a]


def test_supplier_none_and_unknown_filter_safe_fallback(authenticated, state):
    a = raw("Без поставщика")
    supplier = save_supplier(token(), {"name": "Есть", "note": ""})
    b = raw("С поставщиком", supplier=supplier)
    url = reverse("ingredients")
    assert ids(authenticated.get(url, {"supplier": "none"})) == [a]
    for bad in ["not-uuid", str(uuid.uuid4())]:
        response = authenticated.get(url, {"supplier": bad, "sort": "DROP TABLE"})
        assert response.status_code == 200
        assert set(ids(response)) == {a, b}
        assert "supplier" in response.context["list_form"].errors
        assert "sort" in response.context["list_form"].errors
        assert "supplier" not in dict(response.context["list_params"])


def test_dashboard_sorts_exact_cost_price_ratio_and_undefined_last(authenticated, state):
    ingredient = raw("Курица")
    first = save_dish(token(), {"name": "СУП А", "base_price": D(1000)}, [line(ingredient, "0.2")])
    second = save_dish(token(), {"name": "Суп Б", "base_price": D(10)}, [line(ingredient, "0.01")])
    missing = save_dish(token(), {"name": "Суп В", "base_price": D(0)}, [line(ingredient, "0.1")])
    url = reverse("dashboard")
    assert ids(authenticated.get(url, {"q": "суп", "sort": "cost"})) == [second, missing, first]
    assert ids(authenticated.get(url, {"sort": "price"})) == [missing, second, first]
    assert ids(authenticated.get(url, {"sort": "ratio"})) == [first, second, missing]
    assert ids(authenticated.get(url, {"sort": "-ratio"})) == [second, first, missing]
    response = authenticated.get(url, {"zone": "no_price", "q": "СУП", "sort": "-cost", "discount": "10"})
    assert ids(response) == [missing]
    assert dict(response.context["list_params"]) == {"q": "СУП", "zone": "no_price", "sort": "-cost"}


def test_preparation_base_unit_family_and_prices(authenticated, state):
    ingredient = raw("Мука")
    a = save_preparation(token(), {"name": "ТЕСТО А", "output_quantity": D(2000), "output_unit": "g"}, [line(ingredient)])
    b = save_preparation(token(), {"name": "Тесто Б", "output_quantity": D(1), "output_unit": "kg"}, [line(ingredient, "0.8")])
    url = reverse("preparations")
    assert ids(authenticated.get(url, {"q": "тесто", "unit": "kg", "sort": "price"})) == [a, b]
    assert ids(authenticated.get(url, {"unit": "kg", "sort": "cost"})) == [b, a]


def test_supplier_note_search_and_pagination_state(authenticated, state):
    Supplier.objects.bulk_create([Supplier(name=f"Поставщик {i:02}", note="ДОСТАВКА") for i in range(31)])
    response = authenticated.get(reverse("suppliers"), {"q": "доставка", "sort": "-name", "page": "2"})
    assert response.context["list_total"] == 31 and len(response.context["suppliers"]) == 1
    assert response.context["suppliers"][0].name == "Поставщик 00"
    assert response.context["page"].number == 2
    assert parse_qs(response.context["list_query"]) == {"q": ["доставка"], "sort": ["-name"]}
    assert response.context["list_reset_url"] == reverse("suppliers")
    assert authenticated.get(reverse("suppliers"), {"page": "bad"}).context["page"].number == 1


def test_journal_dates_inclusive_in_local_timezone_and_invalid_range(authenticated, state):
    entries = []
    for index, stamp in enumerate(["2026-11-03T18:59:59+00:00", "2026-11-03T19:00:00+00:00", "2026-11-04T18:59:59+00:00", "2026-11-04T19:00:00+00:00"]):
        obj = AuditEntry.objects.create(group=uuid.uuid4(), object_type="Supplier", object_id=str(index), object_name="ПОСТАВЩИК", action="create", after={})
        AuditEntry.objects.filter(pk=obj.pk).update(at=datetime.fromisoformat(stamp))
        entries.append(obj.pk)
    query = {"q": "поставщик", "kind": "Supplier", "from_date": "2026-11-04", "to_date": "2026-11-04", "sort": "oldest"}
    response = authenticated.get(reverse("journal"), query)
    assert [obj.pk for obj in response.context["page"]] == entries[1:3]
    for dates in [{"from_date": "bad"}, {"from_date": "2026-11-05", "to_date": "2026-11-04"}]:
        invalid = authenticated.get(reverse("journal"), dates)
        assert invalid.status_code == 200 and invalid.context["list_form"].errors
        assert invalid.context["list_total"] == 4


@pytest.mark.parametrize("page", ["dashboard", "suppliers", "ingredients", "preparations", "journal"])
def test_invalid_query_does_not_write_domain_data(authenticated, state, page):
    before = state.revision, AuditEntry.objects.count()
    response = authenticated.get(reverse(page), {"q": "я" * 161, "sort": "invalid", "page": "-1"})
    assert response.status_code == 200
    assert set(response.context["list_form"].errors) == {"q", "sort"}
    state.refresh_from_db()
    assert (state.revision, AuditEntry.objects.count()) == before
