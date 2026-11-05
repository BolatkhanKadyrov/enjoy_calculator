from datetime import timedelta
from decimal import Decimal as D
from io import BytesIO
from zipfile import ZipFile
from xml.etree import ElementTree as ET

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from openpyxl import load_workbook

from tseh.models import AuditEntry, Dish, DomainState, Ingredient, Price, PriceApplication, Supplier
from tseh.services.commands import save_dish, save_supplier, save_thresholds
from tseh.services.lifecycle import transition_catalog, transition_dish
from tseh.services.prices import apply_price, create_price
from tseh.services.purchases import save_delivery
from tseh.services.reports import build_xlsx
from tests.test_purchase_web import line_input, purchase
from tests.test_stage2_services import item, line, prep, token

pytestmark = pytest.mark.django_db


def read(data):
    return load_workbook(BytesIO(data), data_only=False)


def rows(book, sheet):
    values = list(book[sheet].values)
    return [dict(zip(values[4], row)) for row in values[5:]]


def facts():
    return (DomainState.objects.get(pk=1).revision, AuditEntry.objects.count(), Price.objects.count(),
            PriceApplication.objects.count(), Dish.objects.count())


def test_report_269_at_discount_contains_nested_recipes_and_all_dish_states_without_writes(state):
    chicken, rice, oil = item(price="2000"), item(price="600"), item(price="900", unit="l")
    Ingredient.objects.filter(pk=chicken).update(name="Курица")
    Ingredient.objects.filter(pk=rice).update(name="Рис")
    Ingredient.objects.filter(pk=oil).update(name="Масло")
    base = prep("Начинка", [line(rice, "0.1"), line(oil, "0.01", "l")])
    dish = save_dish(token(), {"name": "Основное", "base_price": D(1000)},
                     [line(chicken, "100", "g"), line(base, "1", prep=True)])
    archived = save_dish(token(), {"name": "Снятое", "base_price": D(100)}, [line(rice, "0.1")])
    trashed = save_dish(token(), {"name": "Удаляемое", "base_price": D(100)}, [line(rice, "0.1")])
    transition_dish(token(), archived, "archive")
    transition_dish(token(), trashed, "archive")
    transition_dish(token(), trashed, "trash")
    save_thresholds(token(), {"green": D(30), "orange": D(40)})
    before = facts()
    book = read(build_xlsx(D(10)))
    assert facts() == before
    values = {row["Название"]: row for row in rows(book, "Блюда")}
    primary = values["Основное"]
    assert primary["Себестоимость порции, ₸"] == 269
    assert primary["Базовая цена, ₸"] == 1000
    assert primary["Цена со скидкой, ₸"] == 900
    assert primary["Уникальность среди активных"] == 100
    assert primary["Доля себестоимости"] == pytest.approx(29.88888888888889)
    assert primary["Зона"] == "Зелёная зона"
    assert primary["Идентификатор"] == str(dish)
    assert values["Снятое"]["Состояние"] == "Архив"
    assert values["Удаляемое"]["Состояние"] == "Корзина"
    assert values["Удаляемое"]["Срок удаления"] - values["Удаляемое"]["Помещение в корзину"] == timedelta(days=30)
    composition = [row for row in rows(book, "Рецептуры блюд") if row["Блюдо"] == "Основное"]
    assert [(row["Компонент"], row["Количество"], row["Единица"], row["Стоимость строки, ₸"])
            for row in composition] == [("Курица", 100, "г", 200), ("Начинка", 1, "кг", 69)]
    assert rows(book, "Заготовки")[0]["Стоимость замеса, ₸"] == 69
    assert len(rows(book, "Состав заготовок")) == 2
    assert "KZT" in book["Блюда"]["A2"].value and "10%" in book["Блюда"]["A2"].value
    assert "15 значащих" in book["Блюда"]["A3"].value
    for sheet in book:
        assert sheet.freeze_panes == "A6" and sheet.auto_filter.ref.startswith("A5:")
        assert sheet["A5"].alignment.wrap_text and sheet.sheet_view.showGridLines is False


def test_history_preserves_a_b_a_order_and_unapplied_offer_is_only_in_prices(state):
    key = item(price="1000")
    original = Ingredient.objects.get(pk=key).current_price_id
    data = {"supplier_id": None, "price_date": "2026-11-04", "pack_size": D(1),
            "pack_price": D(2000), "comment": "Б", "is_one_off": False}
    create_price(token(), key, data, apply=True)
    b = Ingredient.objects.get(pk=key).current_price_id
    apply_price(token(), key, original)
    create_price(token(), key, {**data, "pack_price": D(9999), "comment": "Не применено"})
    book = read(build_xlsx())
    events = rows(book, "История применений")
    assert [row["ID записи цены"] for row in events] == [str(original), str(b), str(original)]
    assert [row["Цена за базовую единицу, ₸"] for row in events] == [1000, 2000, 1000]
    assert [row["Изменение, ₸"] for row in events] == [None, 1000, -1000]
    assert [row["Изменение"] for row in events] == [None, 100, -50]
    assert len(rows(book, "Цены")) == 3
    assert all(row["Цена за базовую единицу, ₸"] != 9999 for row in events)


def test_purchase_delivery_rows_and_unallocated_delivery_are_explicit(state):
    a, b = item(), item()
    purchase(line_input(a), line_input(b, size="10", count="2"))
    save_delivery(token(), {"amount": D("123.45"), "note": "Позже"})
    book = read(build_xlsx())
    assert rows(book, "Закупки")[0]["Общая доставка, ₸"] == 3000
    values = rows(book, "Строки закупок")
    assert [row["Доля доставки, ₸"] for row in values] == [2000, 1000]
    assert [row["Цена базовой единицы, ₸"] for row in values] == [1100, 550]
    assert [row["Стоимость товара, ₸"] for row in values] == [20000, 10000]
    assert rows(book, "Нераспределённая доставка")[0]["Сумма, ₸"] == 123.45
    assert rows(book, "Нераспределённая доставка")[0]["Заметка"] == "Позже"


def test_xlsx_text_never_becomes_formula_or_error_or_external_link(state):
    texts = ['=HYPERLINK("https://example.invalid","click")', '+SUM(1,2)', '-1+2', '@SUM(A1)', '#N/A', '\t=1+1']
    for value in texts:
        save_supplier(token(), {"name": value, "note": value})
    # PostgreSQL accepts other XML-illegal controls; the report replaces them visibly.
    Supplier.objects.create(name="Control\x01", note="Safe")
    data = build_xlsx()
    book = read(data)
    rendered = {row[0].value: row[0] for row in book["Поставщики"].iter_rows(min_row=6)}
    for value in texts:
        assert rendered[value].data_type == "s" and rendered[value].value == value
    assert "Control�" in rendered
    with ZipFile(BytesIO(data)) as archive:
        assert not any("externalLink" in name for name in archive.namelist())
        for name in archive.namelist():
            if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                tree = ET.fromstring(archive.read(name))
                assert not tree.findall('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f')


def test_tiny_intermediate_price_is_not_rounded_before_final_cost_and_inherited_conflict_survives(state):
    key = item(price="0.01", size="2.5", unit="pc")
    save_thresholds(token(), {"green": D(40), "orange": D(60)})
    save_dish(token(), {"name": "Мелкие детали", "base_price": D(10), "green_override": D(55)},
              [line(key, "1000", "pc")])
    save_thresholds(token(), {"green": D(40), "orange": D(50)})
    book = read(build_xlsx())
    assert rows(book, "Сырьё и упаковка")[0]["Действующая цена за единицу, ₸"] == .004
    values = rows(book, "Блюда")[0]
    assert values["Себестоимость порции, ₸"] == 4
    assert values["Доля себестоимости"] == 40
    assert values["Зона"] == "Некорректные пороги"
    assert values["Зелёный порог"] == 55 and values["Оранжевый порог"] == 50
    assert values["Источник зелёного"] == "Собственный" and values["Источник оранжевого"] == "Общий"
    assert values["Минимальная базовая цена, ₸"] is None


def test_empty_report_and_invalid_discount(state):
    before = facts()
    book = read(build_xlsx())
    assert len(book.sheetnames) == 12 and "Sheet" not in book.sheetnames
    assert book["Блюда"]["A6"].value == "Нет записей"
    assert facts() == before
    for bad in ("NaN", "Infinity", -1, 100, True, 1.5):
        with pytest.raises(ValidationError):
            build_xlsx(bad)


def test_catalog_trash_is_included_with_explicit_state_and_historical_date(state):
    raw = item()
    unused = item(price="321")
    basis = prep("Заготовка в корзине", [line(raw)])
    supplier = save_supplier(token(), {"name": "Поставщик в корзине", "note": ""})
    transition_catalog(token(), "preparation", basis, "trash")
    transition_catalog(token(), "ingredient", unused, "trash")
    transition_catalog(token(), "supplier", supplier, "trash")
    book = read(build_xlsx())
    for sheet, key in (("Заготовки", basis), ("Сырьё и упаковка", unused), ("Поставщики", supplier)):
        exported = next(row for row in rows(book, sheet) if row["Идентификатор"] == str(key))
        assert exported["Состояние"] == "Корзина"
        assert exported["Помещение в корзину"] is not None
        assert "Срок удаления" not in exported
