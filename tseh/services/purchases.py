"""Validated purchase plans and atomic, replay-safe commands.

Every command rebuilds its plan under the common domain lock. Plans are read-only;
the web layer signs the original input and command token before confirmation.
"""
from copy import copy
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import uuid

from django.core.exceptions import ValidationError

from tseh.calculation.purchases import aggregate_lines, allocate_delivery, exact_decimal
from tseh.models import Ingredient, Price, Purchase, PurchaseLine, Supplier, UnallocatedDelivery
from .commands import audit, command, snapshot
from .prices import _apply, _new_price, _price, _validate, application_snapshot, price_snapshot

PURCHASE_FIELDS = ("supplier_id", "purchase_date", "delivery_total", "comment")
LINE_FIELDS = ("ingredient_id", "pack_size", "pack_price", "pack_count", "comment", "is_one_off")


def _clone(obj):
    result = copy(obj)
    result._state = copy(obj._state)
    result._state.fields_cache = dict(obj._state.fields_cache)
    return result


@dataclass
class PurchasePlan:
    purchase: object
    lines: tuple
    prices: dict
    before: dict | None = None
    old_prices: dict = field(default_factory=dict)
    apply_ingredient_ids: tuple = ()
    purchase_id: object = None
    reason: str = ""


@dataclass
class DeletePlan(PurchasePlan):
    deleted_line: object = None
    deleted_price: object = None
    delivery_action: str = ""
    replacement_price: object = None
    remaining_delivery: object = Decimal(0)
    removed_delivery: object = Decimal(0)
    kept_delivery: object = Decimal(0)


@dataclass
class AttachPlan(PurchasePlan):
    delivery: object = None


def _key(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Некорректная ссылка на запись.")


def _decimal(value, *, label, places=12, positive=False, optional=False):
    if optional and (value is None or value == ""):
        return None
    if isinstance(value, (float, bool)) or not isinstance(value, (Decimal, int, str)):
        raise ValidationError(f"{label}: требуется точное десятичное значение.")
    try:
        result = Decimal(value.replace(",", ".") if isinstance(value, str) else value)
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{label}: введите число.")
    if not result.is_finite() or result < 0 or (positive and result == 0) or result >= 10 ** 12:
        raise ValidationError(f"{label}: значение вне допустимого диапазона.")
    if (Fraction(result) * 10 ** places).denominator != 1:
        raise ValidationError(f"{label}: допускается до {places} знаков после запятой.")
    return result


def _text(value, label="Комментарий", required=False):
    if not isinstance(value, str) or len(value) > 2000 or (required and not value.strip()):
        raise ValidationError(f"{label}: укажите текст до 2000 символов.")
    return value


def _mapping(data, allowed):
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise ValidationError("Недопустимые поля операции.")


def _purchase(key):
    obj = Purchase.objects.select_related("supplier").filter(pk=_key(key)).first()
    if obj is None:
        raise ValidationError("Закупка больше не доступна. Обновите страницу.")
    return obj


def _delivery(key):
    obj = UnallocatedDelivery.objects.filter(pk=_key(key)).first()
    if obj is None:
        raise ValidationError("Нераспределённая доставка больше не доступна. Обновите страницу.")
    return obj


def line_snapshot(row):
    result = snapshot(row, ["id", "purchase_id", "position", *LINE_FIELDS, "delivery_share"])
    result.update(ingredient_name=row.ingredient.name, unit=row.ingredient.unit)
    return result


def purchase_snapshot(obj, lines=None):
    result = snapshot(obj, ["id", *PURCHASE_FIELDS, "created_at"])
    result["supplier_name"] = obj.supplier.name if obj.supplier_id else None
    result["lines"] = [line_snapshot(row) for row in (lines if lines is not None else obj.lines.select_related("ingredient"))]
    return result


def _old_prices(purchase):
    return {row.ingredient_id: row for row in purchase.prices.select_related("ingredient", "supplier").prefetch_related("applications")}


def _prices(purchase, lines, old):
    try:
        totals = aggregate_lines(lines)
    except ValueError as exc:
        raise ValidationError(str(exc))
    grouped = {}
    for line in lines:
        grouped.setdefault(line.ingredient_id, []).append(line)
    result = {}
    for key, amounts in totals.items():
        source = grouped[key]
        price = _clone(old[key]) if key in old else Price(ingredient=source[0].ingredient, purchase=purchase)
        price.purchase = purchase
        price.supplier = purchase.supplier
        price.price_date = purchase.purchase_date
        price.pack_size = source[0].pack_size if len(source) == 1 else None
        price.pack_price = source[0].pack_price if len(source) == 1 else None
        price.goods_total = amounts["goods_total"]
        price.quantity_total = amounts["quantity_total"]
        price.delivery_total = amounts["delivery_total"]
        price.comment = source[0].comment if len(source) == 1 else purchase.comment
        price.is_one_off = any(line.is_one_off for line in source)
        price.full_clean(exclude=["purchase", "ingredient", "supplier"], validate_unique=False, validate_constraints=False)
        result[key] = price
    return result


def _redistribute(purchase, lines):
    try:
        shares = allocate_delivery(purchase.delivery_total, lines)
    except ValueError as exc:
        raise ValidationError(str(exc))
    for line in lines:
        line.delivery_share = shares[line.id]


def _allocate_changed_inputs(purchase, lines, original=None, previous_lines=()):
    """Retain deliberately saved shares when their calculation inputs are unchanged.

    Removing a row with ``drop_share`` intentionally leaves a distribution that
    need not equal a fresh proportional allocation. Merely editing metadata (or
    attaching a zero amount) must not replace that preserved distribution.
    """
    def inputs(rows):
        return {row.id: (row.position, row.ingredient_id, row.pack_size, row.pack_price, row.pack_count)
                for row in rows}
    if (original is not None and purchase.delivery_total == original.delivery_total
            and inputs(lines) == inputs(previous_lines)):
        shares = {row.id: row.delivery_share for row in previous_lines}
        for line in lines:
            line.delivery_share = shares[line.id]
        return
    _redistribute(purchase, lines)


def prepare_purchase(data, lines, *, purchase_id=None, apply_ingredient_ids=(), confirm_zero=False, reason=""):
    _mapping(data, PURCHASE_FIELDS)
    if not isinstance(lines, (list, tuple)) or not 1 <= len(lines) <= 100:
        raise ValidationError("Закупка должна содержать от 1 до 100 строк.")
    _text(reason, "Причина исправления")
    original = _purchase(purchase_id) if purchase_id else None
    purchase = _clone(original) if original else Purchase()
    saved = list(original.lines.select_related("ingredient")) if original else []
    before = purchase_snapshot(original, saved) if original else None
    old = _old_prices(original) if original else {}
    for name in PURCHASE_FIELDS:
        if name in data:
            setattr(purchase, name, data[name])
    purchase.delivery_total = _decimal(purchase.delivery_total, label="Доставка", places=2)
    purchase.comment = _text(purchase.comment)
    if original and purchase.delivery_total != original.delivery_total:
        _text(reason, "Причина исправления доставки", required=True)
    if purchase.supplier_id:
        supplier = Supplier.objects.filter(pk=_key(purchase.supplier_id), status="active").first()
        if supplier is None:
            raise ValidationError("Поставщик больше не доступен.")
        purchase.supplier = supplier
    else:
        purchase.supplier = None
    purchase.full_clean(exclude=["supplier"], validate_constraints=False)
    saved_by_id = {row.id: row for row in saved}
    ingredient_ids = []
    for row in lines:
        _mapping(row, (*LINE_FIELDS, "id"))
        ingredient_ids.append(_key(row.get("ingredient_id")))
    ingredients = Ingredient.objects.filter(status="active").in_bulk(ingredient_ids)
    if len(ingredients) != len(set(ingredient_ids)):
        raise ValidationError("Позиция больше не доступна. Обновите страницу.")
    prepared, seen = [], set()
    next_position = max((row.position for row in saved), default=-1) + 1
    for row, ingredient_id in zip(lines, ingredient_ids):
        row_id = _key(row["id"]) if row.get("id") else None
        if row_id and (row_id in seen or row_id not in saved_by_id):
            raise ValidationError("Строка закупки не найдена или указана дважды.")
        if row_id:
            line = _clone(saved_by_id[row_id])
            if line.ingredient_id != ingredient_id:
                raise ValidationError("Позицию сохранённой строки нельзя заменить: добавьте новую строку и удалите прежнюю отдельно.")
            seen.add(row_id)
        else:
            line = PurchaseLine(purchase=purchase, position=next_position)
            next_position += 1
        line.ingredient = ingredients[ingredient_id]
        line.pack_size = _decimal(row.get("pack_size"), label="Фасовка", positive=True)
        line.pack_price = _decimal(row.get("pack_price"), label="Цена упаковки", places=2)
        line.pack_count = _decimal(row.get("pack_count"), label="Количество упаковок", positive=True, optional=True)
        line.comment = _text(row.get("comment", ""))
        line.is_one_off = row.get("is_one_off", False)
        if not isinstance(line.is_one_off, bool):
            raise ValidationError("Некорректная отметка разовой закупки.")
        if line.pack_price == 0 and confirm_zero is not True:
            raise ValidationError("Подтвердите нулевую цену отдельно.")
        prepared.append(line)
    if set(saved_by_id) != seen:
        raise ValidationError("Удаляйте строки отдельным действием с выбором доставки и замены действующей цены.")
    _allocate_changed_inputs(purchase, prepared, original, saved)
    if not isinstance(apply_ingredient_ids, (list, tuple)):
        raise ValidationError("Некорректный набор цен для применения.")
    applied = tuple(_key(key) for key in apply_ingredient_ids)
    if len(applied) != len(set(applied)) or set(applied) - set(ingredients):
        raise ValidationError("Выбраны недопустимые цены для применения.")
    if original and applied:
        raise ValidationError("Исправление закупки не создаёт новых применений. Выберите цену отдельным действием.")
    return PurchasePlan(purchase, tuple(prepared), _prices(purchase, prepared, old), before, old,
                        applied, purchase.id, reason)


def _price_before(price):
    result = price_snapshot(price, price.ingredient)
    result["applications"] = [application_snapshot(event, price, price.ingredient) for event in price.applications.all()]
    return result


def _persist(plan, group):
    purchase = plan.purchase
    purchase.save()
    existing = {row["id"] for row in plan.before["lines"]} if plan.before else set()
    created = [row for row in plan.lines if str(row.id) not in existing]
    changed = [row for row in plan.lines if str(row.id) in existing]
    if created:
        PurchaseLine.objects.bulk_create(created)
    if changed:
        PurchaseLine.objects.bulk_update(changed, [*LINE_FIELDS, "delivery_share"])
    for key, price in plan.prices.items():
        old = plan.old_prices.get(key)
        before = _price_before(old) if old else None
        price.save()
        after = price_snapshot(price, price.ingredient)
        after["reason"] = plan.reason
        audit(group, price, "update" if old else "create", before, after, price.ingredient.name)
    after = purchase_snapshot(purchase, plan.lines)
    after["reason"] = plan.reason
    audit(group, purchase, "update" if plan.before else "create", plan.before, after)
    for key in plan.apply_ingredient_ids:
        _apply(group, plan.prices[key].ingredient, plan.prices[key])


def save_purchase(token, data, lines, *, purchase_id=None, apply_ingredient_ids=(), confirm_zero=False, reason=""):
    payload = {"data": data, "lines": lines, "purchase_id": purchase_id,
               "apply_ingredient_ids": apply_ingredient_ids, "confirm_zero": confirm_zero, "reason": reason}
    def execute(group):
        plan = prepare_purchase(data, lines, purchase_id=purchase_id, apply_ingredient_ids=apply_ingredient_ids,
                                confirm_zero=confirm_zero, reason=reason)
        _persist(plan, group)
        return plan.purchase
    return command(token, "purchase-save", payload, execute)


def prepare_delete_purchase_line(purchase_id, line_id, *, delivery_action, replacement_price_id=None,
                                 replacement_data=None, confirm_zero=False):
    original = _purchase(purchase_id)
    saved = list(original.lines.select_related("ingredient"))
    before = purchase_snapshot(original, saved)
    removed = next((row for row in saved if row.id == _key(line_id)), None)
    if removed is None:
        raise ValidationError("Строка закупки больше не доступна.")
    remaining = [_clone(row) for row in saved if row.id != removed.id]
    old = _old_prices(original)
    deleted_price = old.get(removed.ingredient_id) if not any(row.ingredient_id == removed.ingredient_id for row in remaining) else None
    replacement = None
    current = deleted_price and removed.ingredient.current_price_id == deleted_price.pk
    existing, new = replacement_price_id is not None, replacement_data is not None
    if current:
        if existing == new:
            raise ValidationError("Для удаления действующей цены выберите одну замену или введите новую цену.")
        if existing:
            replacement = _price(removed.ingredient, _key(replacement_price_id))
            if replacement.id == deleted_price.id:
                raise ValidationError("Удаляемая цена не может быть своей заменой.")
        else:
            replacement = _validate(Price(ingredient=removed.ingredient), replacement_data, confirm_zero)
    elif existing or new:
        raise ValidationError("Замена не требуется: действующая цена не удаляется.")
    purchase = _clone(original)
    removed_delivery, kept_delivery = Decimal(0), Decimal(0)
    if not remaining:
        if delivery_action not in {"keep", "delete"}:
            raise ValidationError("Для последней строки выберите: сохранить доставку отдельно или удалить её.")
        if delivery_action == "keep":
            kept_delivery = original.delivery_total
        else:
            removed_delivery = original.delivery_total
        purchase = None
        prices = {}
    else:
        if delivery_action == "remove_all":
            removed_delivery = original.delivery_total
            purchase.delivery_total = Decimal(0)
            for row in remaining:
                row.delivery_share = Decimal(0)
        elif delivery_action == "drop_share":
            removed_delivery = removed.delivery_share
            purchase.delivery_total = exact_decimal(Fraction(original.delivery_total) - Fraction(removed.delivery_share))
        elif delivery_action == "reallocate":
            _redistribute(purchase, remaining)
        else:
            raise ValidationError("Выберите действие с доставкой.")
        prices = _prices(purchase, remaining, old)
    return DeletePlan(purchase, tuple(remaining), prices, before, old, (), original.id, "",
                      removed, deleted_price, delivery_action, replacement,
                      purchase.delivery_total if purchase else Decimal(0), removed_delivery, kept_delivery)


def delete_purchase_line(token, purchase_id, line_id, *, delivery_action, replacement_price_id=None,
                         replacement_data=None, confirm_zero=False):
    payload = {"purchase_id": purchase_id, "line_id": line_id, "delivery_action": delivery_action,
               "replacement_price_id": replacement_price_id, "replacement_data": replacement_data,
               "confirm_zero": confirm_zero}
    def execute(group):
        plan = prepare_delete_purchase_line(purchase_id, line_id, delivery_action=delivery_action,
                    replacement_price_id=replacement_price_id, replacement_data=replacement_data, confirm_zero=confirm_zero)
        deleted = plan.deleted_price
        if plan.replacement_price:
            replacement = plan.replacement_price
            if replacement_data is not None:
                replacement = _new_price(group, plan.deleted_line.ingredient, replacement_data, confirm_zero)
            _apply(group, plan.deleted_line.ingredient, replacement)
        if deleted:
            before = _price_before(deleted)
            for event, event_before in zip(deleted.applications.all(), before["applications"]):
                audit(group, event, "delete", event_before, None, deleted.ingredient.name)
            audit(group, deleted, "delete", before, None, deleted.ingredient.name)
            deleted.applications.all().delete()
            deleted.delete()
        audit(group, plan.deleted_line, "delete", line_snapshot(plan.deleted_line),
              {"delivery_action": delivery_action, "removed_delivery": str(plan.removed_delivery),
               "kept_delivery": str(plan.kept_delivery)}, plan.deleted_line.ingredient.name)
        plan.deleted_line.delete()
        if plan.purchase:
            _persist(plan, group)
            return plan.purchase
        original = _purchase(purchase_id)
        if delivery_action == "keep":
            delivery = UnallocatedDelivery.objects.create(amount=plan.kept_delivery,
                note=f"Из закупки от {original.purchase_date}")
            after = snapshot(delivery, ["id", "amount", "note", "created_at"])
            after["source_purchase"] = str(original.id)
            audit(group, delivery, "create", None, after)
        audit(group, original, "delete", plan.before, {"delivery_action": delivery_action})
        result = _clone(original)
        original.delete()
        return result
    return command(token, "purchase-line-delete", payload, execute)


def prepare_delivery(data, delivery_id=None, reason=""):
    _mapping(data, ("amount", "note"))
    _text(reason, "Причина исправления", required=bool(delivery_id))
    obj = _clone(_delivery(delivery_id)) if delivery_id else UnallocatedDelivery()
    if "amount" in data:
        obj.amount = _decimal(data["amount"], label="Доставка", places=2)
    if "note" in data:
        obj.note = _text(data["note"], "Заметка")
    obj.full_clean()
    return obj


def save_delivery(token, data, delivery_id=None, reason=""):
    def execute(group):
        before = snapshot(_delivery(delivery_id), ["id", "amount", "note", "created_at"]) if delivery_id else None
        obj = prepare_delivery(data, delivery_id, reason)
        obj.save()
        after = snapshot(obj, ["id", "amount", "note", "created_at"])
        after["reason"] = reason
        audit(group, obj, "update" if delivery_id else "create", before, after)
        return obj
    return command(token, "delivery-save", {"data": data, "delivery_id": delivery_id, "reason": reason}, execute)


def delete_delivery(token, delivery_id):
    def execute(group):
        obj = _delivery(delivery_id)
        audit(group, obj, "delete", snapshot(obj, ["id", "amount", "note", "created_at"]), None)
        result = _clone(obj)
        obj.delete()
        return result
    return command(token, "delivery-delete", {"delivery_id": delivery_id}, execute)


def prepare_attach_delivery(delivery_id, purchase_id, *, confirm_addition=False, quantities=None):
    delivery = _delivery(delivery_id)
    original = _purchase(purchase_id)
    if original.delivery_total > 0 and confirm_addition is not True:
        raise ValidationError("В закупке уже есть доставка. Подтвердите добавление отдельного расхода к существующей сумме.")
    purchase = _clone(original)
    purchase.delivery_total = _decimal(exact_decimal(Fraction(original.delivery_total) + Fraction(delivery.amount)), label="Общая доставка", places=2)
    saved = list(original.lines.select_related("ingredient"))
    if not saved:
        raise ValidationError("Нельзя привязать доставку к пустой закупке.")
    rows = [_clone(row) for row in saved]
    if quantities is not None:
        if not isinstance(quantities, dict):
            raise ValidationError("Некорректные количества закупки.")
        values = {_key(key): value for key, value in quantities.items()}
        if set(values) - {row.id for row in rows}:
            raise ValidationError("Количество относится к другой закупке.")
        for row in rows:
            if row.id in values:
                row.pack_count = _decimal(values[row.id], label="Количество упаковок", positive=True)
    _allocate_changed_inputs(purchase, rows, original, saved)
    old = _old_prices(original)
    return AttachPlan(purchase, tuple(rows), _prices(purchase, rows, old),
                      purchase_snapshot(original, saved), old, (), original.id, "", delivery)


def attach_delivery(token, delivery_id, purchase_id, *, confirm_addition=False, quantities=None):
    def execute(group):
        plan = prepare_attach_delivery(delivery_id, purchase_id, confirm_addition=confirm_addition, quantities=quantities)
        _persist(plan, group)
        before = snapshot(plan.delivery, ["id", "amount", "note", "created_at"])
        audit(group, plan.delivery, "transfer", before, {"purchase_id": str(plan.purchase_id),
              "previous_delivery": plan.before["delivery_total"], "transferred_amount": str(plan.delivery.amount),
              "delivery_total": str(plan.purchase.delivery_total)})
        plan.delivery.delete()
        return plan.purchase
    return command(token, "delivery-attach", {"delivery_id": delivery_id, "purchase_id": purchase_id,
                   "confirm_addition": confirm_addition, "quantities": quantities}, execute)
