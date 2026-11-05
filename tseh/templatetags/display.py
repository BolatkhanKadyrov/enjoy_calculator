import json
from decimal import Decimal
from fractions import Fraction

from django import template
from django.utils.html import format_html

from tseh.calculation import money

register = template.Library()


@register.filter
def cash(value):
    rounded = money(value)
    text = f"{rounded:,.2f}".replace(",", "\u00a0").replace(".", ",")
    if Fraction(value) != Fraction(rounded):
        return format_html('<span title="Значение округлено"><span class="approx" aria-label="Приблизительно">~</span>{}</span>', text)
    return text


@register.filter
def goods_unit(price):
    return Fraction(price.goods_total) / Fraction(price.quantity_total) if price.purchase_id else price.effective_unit_price


@register.filter
def delivery_unit(price):
    return Fraction(price.delivery_total) / Fraction(price.quantity_total) if price.purchase_id else Fraction(0)


@register.filter
def number(value):
    text = format(Decimal(value), "f")
    return (text.rstrip("0").rstrip(".") if "." in text else text).replace(".", ",")


@register.filter
def percentage(value):
    """Compact percentage; rounding is presentation-only, just like cash()."""
    if value is None:
        return "—"
    rounded = money(value)
    text = number(rounded)
    if Fraction(value) != Fraction(rounded):
        return format_html('<span title="Значение округлено"><span class="approx" aria-label="Приблизительно">~</span>{}%</span>', text)
    return f"{text}%"


@register.filter
def quantity(line):
    value, unit = line.quantity, line.unit
    if unit == "kg" and value < 1:
        value, unit = value * 1000, "g"
    if unit == "l" and value < 1:
        value, unit = value * 1000, "ml"
    labels = {"g": "г", "kg": "кг", "ml": "мл", "l": "л", "pc": "шт."}
    return f"{number(value)} {labels[unit]}"


@register.filter
def pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2) if value is not None else "—"


@register.filter
def action_label(value):
    return {"create": "Создано", "update": "Изменено", "apply": "Цена применена", "delete": "Удалено", "transfer": "Доставка перенесена",
            "archive": "Перемещено в архив", "trash": "Перемещено в корзину", "restore": "Восстановлено",
            "activate": "Возвращено в ассортимент", "purge": "Удалено окончательно"}.get(value, value)


@register.filter
def object_label(value):
    return {"DomainState": "Восстановление данных", "Purchase": "Закупка", "PurchaseLine": "Строка закупки", "UnallocatedDelivery": "Нераспределённая доставка", "Supplier": "Поставщик", "Ingredient": "Ингредиент / упаковка", "Price": "Цена", "PriceApplication": "Применение цены", "Dish": "Блюдо", "Preparation": "Заготовка", "GlobalSettings": "Общие пороги"}.get(value, value)


@register.filter
def unit_label(value):
    return {"g": "г", "kg": "кг", "ml": "мл", "l": "л", "pc": "шт."}.get(value, value)


@register.filter
def audit_rows(value):
    if not value:
        return []
    labels = {"status": "Состояние", "archived_at": "Последнее помещение в архив", "trashed_at": "Последнее помещение в корзину", "purge_at": "Срок удаления",
              "snapshot_at": "Время снимка", "restored_at": "Восстановлено", "historical_trashed_at": "Историческая дата корзины", "previous_purge_at": "Прежний срок удаления",
              "prices": "Сохранённые цены", "trash_deadlines": "Восстановленные сроки корзины", "counts": "Количество записей",
              "previous_delivery": "Доставка до переноса", "transferred_amount": "Перенесённая сумма", "source_purchase": "Исходная закупка", "purchase_date": "Дата закупки", "pack_count": "Количество упаковок", "delivery_total": "Доставка",
              "delivery_share": "Доля доставки", "goods_total": "Товар: база расчёта", "quantity_total": "Количество: база расчёта",
              "amount": "Сумма доставки", "created_at": "Время создания", "delivery_action": "Действие с доставкой",
              "remaining_delivery": "Оставшаяся доставка", "removed_delivery": "Удалённая доставка", "kept_delivery": "Нераспределённая доставка",
              "purchase_id": "Закупка", "source_delivery_id": "Исходная доставка", "destination_purchase_id": "Целевая закупка",
              "name": "Название", "note": "Контакты / заметка", "unit": "Единица", "loss": "Первичные потери",
              "pack_size": "Содержимое упаковки", "pack_price": "Цена упаковки", "price_date": "Дата цены",
              "base_price": "Отпускная цена", "applied_at": "Время применения", "lines": "Рецептура",
              "comment": "Комментарий", "is_one_off": "Разовая закупка", "reason": "Причина исправления",
              "price": "Запись цены", "applications": "Прежние применения",
              "ingredient_name": "Компонент", "supplier_name": "Поставщик", "output_quantity": "Выход", "output_unit": "Единица выхода",
              "green": "Общий зелёный порог", "orange": "Общий оранжевый порог", "green_override": "Зелёный порог блюда", "orange_override": "Оранжевый порог блюда", "kind": "Тип позиции"}
    rows = []
    for key, item in value.items():
        if key not in labels:
            continue
        if item is None:
            rows.append((labels[key], "—"))
            continue
        if key == "price":
            item = "\n".join(f"{label}: {text}" for label, text in audit_rows(item))
        elif key in {"applications", "prices", "trash_deadlines"}:
            item = "\n\n".join("\n".join(f"{label}: {text}" for label, text in audit_rows(event)) for event in item)
        elif key == "delivery_action":
            item = {"remove_all": "Убрать всю доставку", "drop_share": "Убрать долю удалённой строки", "reallocate": "Перераспределить", "keep": "Сохранить отдельно", "delete": "Удалить доставку"}.get(item, item)
        elif key == "is_one_off":
            item = "Да" if item else "Нет"
        elif key == "status":
            item = {"active": "Активно", "archived": "В архиве", "trashed": "В корзине"}.get(item, item)
        elif key == "counts":
            names = {"settings": "Настройки", "suppliers": "Поставщики", "ingredients": "Сырьё и упаковка", "prices": "Цены",
                "price_applications": "Применения цен", "purchases": "Закупки", "purchase_lines": "Строки закупок",
                "unallocated_delivery": "Доставка", "preparations": "Заготовки", "preparation_lines": "Строки заготовок",
                "dishes": "Блюда", "dish_lines": "Строки блюд", "journal": "Журнал"}
            item = "\n".join(f"{names.get(name, name)}: {count}" for name, count in item.items())
        elif key == "lines":
            if item and "pack_size" in item[0]:
                item = "\n\n".join("\n".join(f"{label}: {text}" for label, text in audit_rows(row)) for row in item)
            else:
                item = "\n".join(f"{row.get('ingredient_name', 'Ингредиент')}: {number(row['quantity'])} {unit_label(row['unit'])}" for row in item)
        elif key in {"unit", "output_unit"}:
            item = unit_label(item)
        elif key in {"pack_price", "base_price", "delivery_total", "delivery_share", "amount", "remaining_delivery", "removed_delivery", "kept_delivery", "previous_delivery", "transferred_amount"}:
            item = f"{cash(Decimal(item))} ₸"
        elif key == "loss":
            item = f"{number(item)}%"
        elif key in {"pack_size", "output_quantity", "pack_count", "goods_total", "quantity_total"}:
            item = number(item)
        elif key in {"green", "orange", "green_override", "orange_override"}:
            item = f"{number(item)}%" if item is not None else ("По умолчанию" if key.endswith("_override") else "Не настроен")
        elif key == "kind":
            item = "Упаковка" if item == "packaging" else "Ингредиент"
        elif key in {"applied_at", "created_at", "archived_at", "trashed_at", "purge_at", "snapshot_at", "restored_at", "historical_trashed_at", "previous_purge_at"}:
            from datetime import datetime
            from django.utils import timezone
            item = timezone.localtime(datetime.fromisoformat(item)).strftime("%d.%m.%Y %H:%M:%S")
        rows.append(("Строки закупки" if key == "lines" and "purchase_date" in value else labels[key], item or "—"))
    return rows
