"""Acceptance-sized data, not a concurrent-user or Railway load benchmark.

The independent paper oracle is deliberately simple: raw materials cost 1000/kg;
A costs 1000 / 2 = 500/kg, B costs (0.5*500 + 0.25*1000) / 1
= 500/kg, C costs (0.4*500 + 0.2*500) / 0.5 = 600/kg.
Every dish contains five different 100-g raw portions plus 100 g of C:
5*100 + 60 = 560.00. No production calculator constructs these expectations.
"""
from datetime import date
from decimal import Decimal as D
from io import BytesIO
from time import perf_counter

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from openpyxl import load_workbook

from tseh.models import AuditEntry, DomainState
from tseh.services import backups
from tseh.services.commands import (
    create_ingredient, save_dish, save_preparation, save_supplier,
    save_thresholds, stamp,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def target_assortment(state):
    def token():
        return stamp(DomainState.objects.get(pk=state.pk))

    def raw(key, quantity):
        return {"ingredient_id": key, "quantity": D(quantity), "unit": "kg"}

    def prepared(key, quantity):
        return {"preparation_id": key, "quantity": D(quantity), "unit": "kg"}

    suppliers = [save_supplier(token(), {"name": f"Поставщик {i + 1}", "note": ""}) for i in range(4)]
    ingredients = [create_ingredient(token(), {
        "name": f"Сырьё {i + 1:03}", "kind": "ingredient", "unit": "kg", "loss": D(0),
        "supplier_id": suppliers[i % 4], "pack_size": D(1), "pack_price": D(1000),
        "price_date": date(2026, 11, 5),
    }) for i in range(150)]
    a = save_preparation(token(), {"name": "Основа А", "output_quantity": D(2), "output_unit": "kg"},
                         [raw(ingredients[0], "1")])
    b = save_preparation(token(), {"name": "Основа Б", "output_quantity": D(1), "output_unit": "kg"},
                         [prepared(a, "0.5"), raw(ingredients[1], "0.25")])
    c = save_preparation(token(), {"name": "Основа В", "output_quantity": D("0.5"), "output_unit": "kg"},
                         [prepared(b, "0.4"), prepared(a, "0.2")])
    dishes = [save_dish(token(), {"name": f"Блюдо {i + 1:02}", "base_price": D(1000)},
                        [raw(key, "0.1") for key in ingredients[i * 5:i * 5 + 5]] + [prepared(c, "0.1")])
              for i in range(30)]
    save_thresholds(token(), {"green": D(40), "orange": D(60)})
    return {"suppliers": suppliers, "ingredients": ingredients, "preparations": [a, b, c], "dishes": dishes}


def test_target_scale_dashboard_json_and_xlsx_preserve_records_and_known_costs(authenticated, target_assortment):
    before = (DomainState.objects.get(pk=1).revision, AuditEntry.objects.count())
    timings = {}
    start = perf_counter()
    with CaptureQueriesContext(connection) as queries:
        response = authenticated.get(reverse("dashboard"), {"discount": "10", "sort": "name"})
    dashboard_queries = len(queries)
    timings["dashboard"] = perf_counter() - start
    assert response.status_code == 200
    assert response.context["list_total"] == 30 and len(response.context["rows"]) == 30
    assert {row[0].pk for row in response.context["rows"]} == set(target_assortment["dishes"])
    for _, result, assessment in response.context["rows"]:
        assert result.total == D("560.00")
        assert assessment.effective == D("900.00")
        assert assessment.zone == "red"
    # This bound catches accidental per-item queries without prescribing timing.
    assert 0 < dashboard_queries <= 30

    start = perf_counter()
    exported = authenticated.get(reverse("export-json"))
    timings["json_export"] = perf_counter() - start
    assert exported.status_code == 200
    start = perf_counter()
    document = backups.validate_backup(exported.content)
    timings["json_validate"] = perf_counter() - start
    for name, identifiers in target_assortment.items():
        assert {row["id"] for row in document["data"][name]} == {str(key) for key in identifiers}
    assert len(document["data"]["prices"]) == len(document["data"]["price_applications"]) == 150
    assert len(document["data"]["dish_lines"]) == 180
    assert len(document["data"]["preparation_lines"]) == 5
    assert len(document["data"]["journal"]) == before[1]

    start = perf_counter()
    report = authenticated.get(reverse("export-xlsx"))
    timings["xlsx_export"] = perf_counter() - start
    assert report.status_code == 200
    book = load_workbook(BytesIO(report.content), read_only=True, data_only=True)
    try:
        def rows(name):
            values = list(book[name].values)
            return [dict(zip(values[4], row)) for row in values[5:]]

        for table, sheet in [("dishes", "Блюда"), ("ingredients", "Сырьё и упаковка"),
                             ("preparations", "Заготовки"), ("suppliers", "Поставщики")]:
            assert {row["Идентификатор"] for row in rows(sheet)} == {str(key) for key in target_assortment[table]}
        assert len(rows("Цены")) == len(rows("История применений")) == 150
        assert len(rows("Рецептуры блюд")) == 180 and len(rows("Состав заготовок")) == 5
        for dish in rows("Блюда"):
            assert dish["Себестоимость порции, ₸"] == 560
            assert dish["Цена со скидкой, ₸"] == 900
            assert dish["Зона"] == "Красная зона"
    finally:
        book.close()
    assert (DomainState.objects.get(pk=1).revision, AuditEntry.objects.count()) == before
    print("ACCEPTANCE_SCALE 30 dishes / 150 ingredients / 4 suppliers / 3 nested preparations; "
          + "; ".join(f"{name}={elapsed:.3f}s" for name, elapsed in timings.items())
          + f"; dashboard_queries={dashboard_queries}; json_bytes={len(exported.content)}; xlsx_bytes={len(report.content)}")
