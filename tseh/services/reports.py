"""Human-readable XLSX reports. JSON, not Excel, is the lossless backup format.

Call build_xlsx within the caller's read_snapshot so the report represents one
consistent domain revision. All arithmetic is delegated to the exact core;
conversion to Excel numbers happens only when writing report cells.
"""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from io import BytesIO
import math
import re

from django.core.exceptions import ValidationError
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from tseh.calculation.pricing import assess, effective_thresholds
from tseh.models import (Dish, Ingredient, Preparation, Price, PriceApplication,
                         Purchase, Supplier, UnallocatedDelivery)
from .price_queries import price_history
from .queries import catalog, components, general_settings
from .uniqueness import uniqueness_report

MONEY = '#,##0.00'
QUANTITY = '0.###############'
PERCENT = '0.00"%"'
DATE = 'dd.mm.yyyy'
TIMESTAMP = 'dd.mm.yyyy hh:mm:ss'
UNITS = dict(Ingredient.UNITS) | {"g": "г", "ml": "мл"}
STATES = {"active": "Активно", "archived": "Архив", "trashed": "Корзина"}
PRECISION_NOTE = ("Отчёт Excel: числа ограничены точностью Excel (около 15 значащих цифр). "
                  "Денежные колонки показаны до сотых; промежуточные значения не заменяют точные расчёты сервиса. "
                  "Для полного переноса и восстановления скачайте JSON.")
ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


@dataclass(frozen=True)
class Column:
    name: str
    width: int = 24
    number_format: str | None = None


def _cell_value(cell, value, number_format=None):
    """Write all supplied text as literal text, even =, +, -, @ and #N/A."""
    if isinstance(value, str):
        cell.value = ILLEGAL_XML.sub("\ufffd", value)
        cell.data_type = "s"
    elif isinstance(value, (Decimal, Fraction)):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            raise ValidationError("Одно из значений выходит за числовые пределы Excel. Используйте полный JSON-экспорт.")
        if not math.isfinite(number) or (number == 0 and value != 0):
            raise ValidationError("Одно из значений выходит за числовые пределы Excel. Используйте полный JSON-экспорт.")
        cell.value = number
    elif isinstance(value, datetime):
        cell.value = timezone.localtime(value).replace(tzinfo=None) if timezone.is_aware(value) else value
        number_format = TIMESTAMP
    else:
        cell.value = value
        if isinstance(value, date):
            number_format = DATE
    if number_format:
        cell.number_format = number_format


class Report:
    def __init__(self, discount):
        self.book = Workbook()
        self.book.remove(self.book.active)
        self.at = timezone.localtime(timezone.now())
        self.discount = discount
        self.book.properties.title = "Цех — отчёт по себестоимости"
        self.book.properties.creator = "Цех"
        self.book.properties.description = "Отчёт, не резервная копия. Для восстановления используйте JSON."

    def sheet(self, title, columns, rows):
        ws = self.book.create_sheet(title)
        last = get_column_letter(len(columns))
        metadata = (f"Выгрузка: {self.at:%d.%m.%Y %H:%M:%S} ({timezone.get_current_timezone_name()}); "
                    f"валюта: KZT / ₸; скидка анализа: {self.discount}%.")
        for row, text, size in ((1, title, 18), (2, metadata, 11), (3, PRECISION_NOTE, 11)):
            ws.merge_cells(f"A{row}:{last}{row}")
            _cell_value(ws.cell(row, 1), text)
            ws.cell(row, 1).font = Font(name="Calibri", size=size, bold=row == 1, color="24322B")
            ws.cell(row, 1).alignment = Alignment(vertical="center", wrap_text=True)
            ws.row_dimensions[row].height = 30 if row == 1 else 36
        header_fill = PatternFill("solid", fgColor="30553D")
        border = Border(bottom=Side(style="hair", color="DCE2D5"))
        for index, column in enumerate(columns, 1):
            cell = ws.cell(5, index)
            _cell_value(cell, column.name)
            cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            ws.column_dimensions[get_column_letter(index)].width = column.width
        ws.row_dimensions[5].height = 48
        count = 0
        for count, values in enumerate(rows, 1):
            height = 30
            for index, (column, value) in enumerate(zip(columns, values, strict=True), 1):
                cell = ws.cell(count + 5, index)
                _cell_value(cell, value, column.number_format)
                cell.font = Font(name="Calibri", size=11, color="24322B")
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                cell.border = border
                if count % 2 == 0:
                    cell.fill = PatternFill("solid", fgColor="F5F5F0")
                if isinstance(value, str):
                    lines = sum(max(1, math.ceil(len(part) / (column.width * .85))) for part in value.split("\n"))
                    height = max(height, min(409, 15 * lines + 8))
            ws.row_dimensions[count + 5].height = height
        if not count:
            _cell_value(ws.cell(6, 1), "Нет записей")
            ws.cell(6, 1).font = Font(name="Calibri", size=11, italic=True, color="58664E")
        ws.freeze_panes = "A6"
        ws.auto_filter.ref = f"A5:{last}{max(5, 5 + count)}"
        ws.sheet_view.showGridLines = False
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.print_title_rows = "1:5"
        ws.oddFooter.center.text = "Цех · &P / &N"
        return ws

    def bytes(self):
        stream = BytesIO()
        self.book.save(stream)
        return stream.getvalue()


def build_xlsx(discount=0):
    """Return one report containing all states; no domain writes or formulas."""
    if isinstance(discount, (bool, float)):
        raise ValidationError("Скидка отчёта должна быть точным числом от 0 до значения меньше 100%.")
    try:
        discount = Decimal(discount)
        if not discount.is_finite() or not 0 <= discount < 100:
            raise ValueError
    except (ValueError, TypeError, ArithmeticError):
        raise ValidationError("Скидка отчёта должна быть от 0 до значения меньше 100%.")
    report = Report(discount)
    graph, settings = catalog(), general_settings()
    items = list(Ingredient.objects.select_related("current_price__supplier").order_by("name", "id"))
    item_map = {item.pk: item for item in items}
    preparations = list(Preparation.objects.prefetch_related("lines").order_by("name", "id"))
    prep_map = {prep.pk: prep for prep in preparations}
    dishes = list(Dish.objects.prefetch_related("lines").order_by("status", "name", "id"))
    uniqueness = uniqueness_report().dishes
    suppliers = list(Supplier.objects.all())
    supplier_map = {supplier.pk: supplier.name for supplier in suppliers}
    prices = list(Price.objects.select_related("ingredient").order_by("ingredient__name", "price_date", "created_at", "id"))
    price_map = {price.pk: price for price in prices}
    purchases = list(Purchase.objects.prefetch_related("lines").order_by("purchase_date", "created_at", "id"))
    applications = list(PriceApplication.objects.select_related("price").order_by("applied_at", "id"))
    events = {item.pk: [] for item in items}
    for event in applications:
        events[event.price.ingredient_id].append(event)
    by_event = {row.id: row for item in items for row in price_history(item, events=events[item.pk])}
    name = Column("Название", 32)
    state = Column("Состояние", 16)
    ident = Column("Идентификатор", 38)
    supplier = Column("Поставщик", 32)
    date_col = Column("Дата", 14)
    comment = Column("Комментарий", 96)
    money_col = lambda label: Column(label, 20, MONEY)
    quantity_col = lambda label: Column(label, 23, QUANTITY)
    percent_col = lambda label: Column(label, 20, PERCENT)
    info_rows = [
        ("Назначение", "Отчёт по действующим ценам на дату выгрузки. Архив и корзина включены с явным состоянием."),
        ("Полный перенос", "JSON сохраняет точность, связи и журнал. XLSX не предназначен для восстановления; импорта Excel нет."),
        ("История", "Лист истории содержит только события применения. Неприменённые предложения находятся на листе цен."),
        ("Состав себестоимости", "Сырьё, упаковка и доставка поставщика. Остальные накладные расходы отдельно не учитываются."),
        ("Скидка анализа, %", discount),
        ("Общий зелёный порог, %", settings.green),
        ("Общий оранжевый порог, %", settings.orange),
        ("Сохранность", "Скачивайте JSON регулярно вручную. В отчёте нет учётных данных, ключей и сессий."),
        ("Текст", "Текстовые значения записаны как текст, без формул. Недопустимые для XML управляющие символы заменены знаком �."),
    ]
    report.sheet("Об отчёте", [Column("Параметр", 32), Column("Значение", 110)], info_rows)
    dish_rows, recipe_rows = [], []
    def component_fields(row):
        obj = item_map[row.ingredient_id] if row.ingredient_id else prep_map[row.preparation_id]
        kind = ("Упаковка" if obj.kind == "packaging" else "Сырьё") if row.ingredient_id else "Заготовка"
        return kind, obj.name
    for dish in dishes:
        lines = list(dish.lines.all())
        result = graph.calculate(components(lines))
        green, orange = effective_thresholds(dish, settings)
        assessment = assess(result.total, dish.base_price, discount, green, orange)
        dish_rows.append((dish.name, STATES[dish.status], result.total, dish.base_price, assessment.effective,
            uniqueness[dish.pk].percentage, assessment.ratio, assessment.label, green, orange,
            "Общий" if dish.green_override is None else "Собственный",
            "Общий" if dish.orange_override is None else "Собственный", assessment.minimum,
            dish.archived_at, dish.trashed_at, dish.purge_at, str(dish.pk)))
        for line, value in zip(lines, result.rows, strict=True):
            recipe_rows.append((dish.name, STATES[dish.status], line.position + 1, *component_fields(line),
                line.quantity, UNITS[line.unit], value, str(dish.pk)))
    report.sheet("Блюда", [name, state, money_col("Себестоимость порции, ₸"), money_col("Базовая цена, ₸"),
        money_col("Цена со скидкой, ₸"), percent_col("Уникальность среди активных"),
        percent_col("Доля себестоимости"), Column("Зона", 28),
        percent_col("Зелёный порог"), percent_col("Оранжевый порог"), Column("Источник зелёного", 19),
        Column("Источник оранжевого", 19), money_col("Минимальная базовая цена, ₸"),
        Column("Архивация", 23), Column("Помещение в корзину", 23), Column("Срок удаления", 23), ident], dish_rows)
    recipe_columns = [Column("Блюдо", 32), state, Column("Строка", 10), Column("Тип компонента", 18),
        Column("Компонент", 32), quantity_col("Количество"), Column("Единица", 12),
        money_col("Стоимость строки, ₸"), ident]
    report.sheet("Рецептуры блюд", recipe_columns, recipe_rows)
    prep_rows, prep_recipe_rows = [], []
    for prep in preparations:
        lines = list(prep.lines.all())
        result = graph.prep_costs[prep.pk]
        prep_rows.append((prep.name, prep.output_quantity, UNITS[prep.output_unit], result.raw,
                          graph.prep_prices[prep.pk], UNITS[prep.unit], str(prep.pk)))
        for line, value in zip(lines, result.rows, strict=True):
            prep_recipe_rows.append((prep.name, line.position + 1, *component_fields(line), line.quantity,
                                     UNITS[line.unit], value, str(prep.pk)))
    report.sheet("Заготовки", [name, quantity_col("Выход замеса"), Column("Единица выхода", 16),
        money_col("Стоимость замеса, ₸"), money_col("Цена базовой единицы, ₸"), Column("Базовая единица", 16),
        ident, state, Column("Помещение в корзину", 23)],
        ((*values, STATES[prep.status], prep.trashed_at) for prep, values in zip(preparations, prep_rows, strict=True)))
    report.sheet("Состав заготовок", [Column("Заготовка", 32), *recipe_columns[2:]], prep_recipe_rows)
    report.sheet("Сырьё и упаковка", [name, Column("Тип", 16), Column("Базовая единица", 16),
        percent_col("Первичные потери"), money_col("Действующая цена за единицу, ₸"), supplier,
        Column("Дата действующей цены", 16), Column("ID действующей цены", 38), ident, state,
        Column("Помещение в корзину", 23)],
        ((item.name, "Упаковка" if item.kind == "packaging" else "Сырьё", UNITS[item.unit], item.loss,
          item.current_price.effective_unit_price, supplier_map.get(item.current_price.supplier_id, "Не указан"),
          item.current_price.price_date, str(item.current_price_id), str(item.pk), STATES[item.status], item.trashed_at)
         for item in items))
    report.sheet("Цены", [Column("Позиция", 32), Column("Действующая", 15), date_col, supplier,
        quantity_col("Содержимое упаковки"), Column("Единица", 12), money_col("Цена упаковки без доставки, ₸"),
        money_col("Стоимость товара в закупке, ₸"), quantity_col("Количество в закупке, баз. ед."),
        money_col("Доставка позиции, ₸"), money_col("Цена базовой единицы с доставкой, ₸"),
        Column("Разовая закупка", 16), comment, Column("Внесено", 23), Column("ID закупки", 38), ident],
        ((price.ingredient.name, "Да" if item_map[price.ingredient_id].current_price_id == price.pk else "Нет",
          price.price_date, supplier_map.get(price.supplier_id, "Не указан"), price.pack_size,
          UNITS[price.ingredient.unit], price.pack_price, price.goods_total, price.quantity_total, price.delivery_total,
          price.effective_unit_price, "Да" if price.is_one_off else "Нет", price.comment,
          price.created_at, str(price.purchase_id) if price.purchase_id else None, str(price.pk)) for price in prices))
    report.sheet("История применений", [Column("Событие", 14), Column("Применено", 23), Column("Позиция", 32),
        money_col("Цена за базовую единицу, ₸"), Column("Единица", 12), money_col("Изменение, ₸"),
        percent_col("Изменение"), date_col, supplier, Column("ID записи цены", 38)],
        ((str(event.pk), event.applied_at, item_map[event.price.ingredient_id].name, by_event[event.pk].unit_price,
          UNITS[item_map[event.price.ingredient_id].unit], by_event[event.pk].absolute_change,
          by_event[event.pk].change, price_map[event.price_id].price_date,
          supplier_map.get(price_map[event.price_id].supplier_id, "Не указан"), str(event.price_id))
         for event in applications))
    report.sheet("Поставщики", [name, Column("Контакты / заметка", 96), ident, state, Column("Помещение в корзину", 23)],
        ((obj.name, obj.note, str(obj.pk), STATES[obj.status], obj.trashed_at) for obj in suppliers))
    report.sheet("Закупки", [date_col, supplier, money_col("Общая доставка, ₸"), Column("Строк", 12),
        comment, Column("Внесено", 23), ident],
        ((obj.purchase_date, supplier_map.get(obj.supplier_id, "Не указан"), obj.delivery_total,
          len(obj.lines.all()), obj.comment, obj.created_at, str(obj.pk)) for obj in purchases))
    report.sheet("Строки закупок", [Column("Дата закупки", 16), supplier, Column("Строка", 10),
        Column("Позиция", 32), quantity_col("Число упаковок"), quantity_col("Содержимое упаковки"),
        Column("Единица", 12), money_col("Цена упаковки, ₸"), money_col("Стоимость товара, ₸"),
        money_col("Доля доставки, ₸"), money_col("Цена базовой единицы, ₸"), Column("Разовая закупка", 16),
        comment, Column("ID закупки", 38), ident],
        ((purchase.purchase_date, supplier_map.get(purchase.supplier_id, "Не указан"), line.position + 1,
          item_map[line.ingredient_id].name, line.pack_count, line.pack_size, UNITS[item_map[line.ingredient_id].unit],
          line.pack_price, line.goods_total, line.delivery_share, line.effective_unit_price,
          "Да" if line.is_one_off else "Нет", line.comment, str(purchase.pk), str(line.pk))
         for purchase in purchases for line in purchase.lines.all()))
    report.sheet("Нераспределённая доставка", [money_col("Сумма, ₸"), Column("Создано", 23),
        Column("Заметка", 96), ident], ((obj.amount, obj.created_at, obj.note, str(obj.pk))
        for obj in UnallocatedDelivery.objects.order_by("created_at", "id")))
    return report.bytes()
