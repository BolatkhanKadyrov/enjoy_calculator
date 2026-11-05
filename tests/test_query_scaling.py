"""Query counts must not grow with rows in routinely large screens."""
from decimal import Decimal as D
import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from tseh.forms import LineFormSet
from tseh.models import AuditEntry, Dish, DishLine
from tests.test_stage2_services import item

pytestmark = pytest.mark.django_db


def test_recipe_catalog_loaded_once_for_one_or_hundred_rows(state):
    key = item()
    initial = {"ingredient": key, "quantity": D("100"), "unit": "g"}
    counts = []
    for count in (1, 100):
        with CaptureQueriesContext(connection) as queries:
            forms = LineFormSet(initial=[initial.copy() for _ in range(count)])
            rendered = "".join(form.as_p() for form in forms) + forms.empty_form.as_p()
        assert "form-0-component" in rendered
        counts.append(len(queries))
    assert counts[1] <= counts[0] + 1
    assert counts[1] <= 3


def test_dashboard_queries_do_not_grow_per_dish(authenticated, state):
    key = item()
    def add_dishes(count):
        dishes = Dish.objects.bulk_create([Dish(name=f"Блюдо {uuid.uuid4()}", base_price=1000) for _ in range(count)])
        DishLine.objects.bulk_create([DishLine(dish=dish, ingredient_id=key, quantity=D("0.1"), unit="kg", position=0) for dish in dishes])
    add_dishes(1)
    authenticated.get(reverse("dashboard"))
    with CaptureQueriesContext(connection) as small:
        assert authenticated.get(reverse("dashboard")).status_code == 200
    add_dishes(34)
    with CaptureQueriesContext(connection) as large:
        response = authenticated.get(reverse("dashboard"))
    assert response.status_code == 200
    assert response.context["list_total"] == 35
    assert len(large) <= len(small) + 1


@pytest.mark.parametrize("query", ["", "закупка"])
def test_journal_fetches_large_snapshots_only_for_current_page(authenticated, state, query):
    AuditEntry.objects.bulk_create([AuditEntry(group=uuid.uuid4(), object_type="Purchase", object_id=str(uuid.uuid4()),
        object_name=f"Закупка {i}", action="update", before={"comment": "x" * 3000}, after={"comment": "y" * 3000}) for i in range(120)])
    with CaptureQueriesContext(connection) as queries:
        response = authenticated.get(reverse("journal"), {"q": query})
    assert response.status_code == 200
    assert response.context["list_total"] == 120
    assert len(response.context["page"].object_list) == 30
    snapshot_selects = [q["sql"] for q in queries if '"tseh_auditentry"."before"' in q["sql"]]
    assert len(snapshot_selects) == 1
    assert "LIMIT 30" in snapshot_selects[0]
