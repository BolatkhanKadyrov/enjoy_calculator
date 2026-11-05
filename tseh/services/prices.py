"""Atomic price actions. A saved offer and an application are separate facts."""
from decimal import Decimal

from django.core.exceptions import ValidationError

from tseh.models import Ingredient, Price, PriceApplication
from .commands import audit, command, snapshot, validate_active_supplier

PRICE_FIELDS = ("supplier_id", "price_date", "pack_size", "pack_price", "comment", "is_one_off")


def _ingredient(key):
    obj = Ingredient.objects.filter(pk=key).first()
    if obj is None or obj.status != "active":
        raise ValidationError("Позиция больше не доступна. Обновите страницу.")
    return obj


def _price(ingredient, key):
    obj = Price.objects.select_related("supplier").filter(pk=key, ingredient=ingredient).first()
    if obj is None:
        raise ValidationError("Цена не найдена или принадлежит другой позиции.")
    return obj


def _validate(price, data, confirm_zero):
    if not isinstance(data, dict) or set(data) - set(PRICE_FIELDS):
        raise ValidationError("Недопустимые поля цены. Позицию существующей цены менять нельзя.")
    for key, value in data.items():
        if key in {"pack_size", "pack_price"} and isinstance(value, (float, bool)):
            raise ValidationError("Цена и фасовка должны быть точными десятичными значениями.")
        if key == "is_one_off" and not isinstance(value, bool):
            raise ValidationError("Некорректная отметка разовой закупки.")
        if key == "comment" and (not isinstance(value, str) or len(value) > 2000):
            raise ValidationError("Комментарий должен быть текстом до 2000 символов.")
        setattr(price, key, value)
    price.full_clean()
    if price.ingredient.status != "active":
        raise ValidationError("Позиция находится в корзине. Сначала восстановите её.")
    validate_active_supplier(price.supplier_id)
    if price.pack_price == Decimal(0) and confirm_zero is not True:
        raise ValidationError("Нулевая цена снизит себестоимость. Подтвердите нулевую цену отдельно.")
    return price


def price_snapshot(price, ingredient):
    result = snapshot(price, ["id", "ingredient_id", "supplier_id", "price_date", "created_at",
                              "pack_size", "pack_price", "comment", "is_one_off", "purchase_id",
                              "goods_total", "quantity_total", "delivery_total"])
    result.update(ingredient_name=ingredient.name, unit=ingredient.unit,
                  supplier_name=price.supplier.name if price.supplier_id else None)
    return result


def application_snapshot(event, price, ingredient):
    return {**snapshot(event, ["id", "price_id", "applied_at"]), "ingredient_id": str(ingredient.pk),
            "ingredient_name": ingredient.name, "price": price_snapshot(price, ingredient)}


def _new_price(group, ingredient, data, confirm_zero):
    price = _validate(Price(ingredient=ingredient), data, confirm_zero)
    price.save(force_insert=True)
    audit(group, price, "create", None, price_snapshot(price, ingredient), ingredient.name)
    return price


def _apply(group, ingredient, price):
    before = snapshot(ingredient, ["name", "current_price_id"])
    ingredient.current_price = price
    ingredient.save(update_fields=["current_price"])
    audit(group, ingredient, "update", before, snapshot(ingredient, ["name", "current_price_id"]))
    event = PriceApplication.objects.create(price=price)
    audit(group, event, "apply", None, application_snapshot(event, price, ingredient), ingredient.name)


def create_price(token, ingredient_id, data, *, apply=False, confirm_zero=False):
    payload = {"ingredient_id": ingredient_id, "data": data, "apply": apply, "confirm_zero": confirm_zero}
    def execute(group):
        if not isinstance(apply, bool):
            raise ValidationError("Некорректный режим применения цены.")
        ingredient = _ingredient(ingredient_id)
        price = _new_price(group, ingredient, data, confirm_zero)
        if apply:
            _apply(group, ingredient, price)
        return ingredient
    return command(token, "price-create", payload, execute)


def apply_price(token, ingredient_id, price_id):
    def execute(group):
        ingredient = _ingredient(ingredient_id)
        price = _price(ingredient, price_id)
        if ingredient.current_price_id == price.pk:
            raise ValidationError("Эта цена уже действует.")
        _apply(group, ingredient, price)
        return ingredient
    return command(token, "price-apply", {"ingredient_id": ingredient_id, "price_id": price_id}, execute)


def correct_price(token, ingredient_id, price_id, data, *, confirm_zero=False, reason=""):
    def execute(group):
        if not isinstance(reason, str) or len(reason) > 2000:
            raise ValidationError("Причина исправления должна быть текстом до 2000 символов.")
        ingredient = _ingredient(ingredient_id)
        price = _price(ingredient, price_id)
        if price.purchase_id:
            raise ValidationError("Эта цена связана с закупкой. Исправьте исходные строки и доставку в закупке.")
        before = price_snapshot(price, ingredient)
        before["applications"] = [application_snapshot(event, price, ingredient) for event in price.applications.all()]
        _validate(price, data, confirm_zero)
        price.save(update_fields=list(PRICE_FIELDS))
        after = price_snapshot(price, ingredient)
        after["reason"] = reason
        audit(group, price, "update", before, after, ingredient.name)
        return ingredient
    return command(token, "price-correct", {"ingredient_id": ingredient_id, "price_id": price_id,
        "data": data, "confirm_zero": confirm_zero, "reason": reason}, execute)


def delete_price(token, ingredient_id, price_id, *, replacement_price_id=None, replacement_data=None, confirm_zero=False):
    def execute(group):
        ingredient = _ingredient(ingredient_id)
        price = _price(ingredient, price_id)
        if price.purchase_id:
            raise ValidationError("Эта цена связана с закупкой. Удалите исходную строку в закупке с выбором действия для доставки.")
        existing, new = replacement_price_id is not None, replacement_data is not None
        is_current = ingredient.current_price_id == price.pk
        if is_current and existing == new:
            raise ValidationError("Для удаления действующей цены выберите одну замену или введите новую цену.")
        if not is_current and (existing or new):
            raise ValidationError("Для недействующей цены замена не требуется.")
        before = price_snapshot(price, ingredient)
        events = list(price.applications.all())
        before["applications"] = [application_snapshot(event, price, ingredient) for event in events]
        if is_current:
            if existing:
                replacement = _price(ingredient, replacement_price_id)
                if replacement.pk == price.pk:
                    raise ValidationError("Удаляемая цена не может быть своей заменой.")
            else:
                replacement = _new_price(group, ingredient, replacement_data, confirm_zero)
            _apply(group, ingredient, replacement)
        # Preserve the IDs and original facts before removing any protected references.
        for event, event_before in zip(events, before["applications"]):
            audit(group, event, "delete", event_before, None, ingredient.name)
        audit(group, price, "delete", before, None, ingredient.name)
        price.applications.all().delete()
        price.delete()
        return ingredient
    return command(token, "price-delete", {"ingredient_id": ingredient_id, "price_id": price_id,
        "replacement_price_id": replacement_price_id, "replacement_data": replacement_data,
        "confirm_zero": confirm_zero}, execute)
