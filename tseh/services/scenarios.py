"""A scenario chooses prices together; source purchases remain independent facts."""
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
import uuid

from django.core.exceptions import ValidationError

from tseh.models import Ingredient, Price, Purchase
from .commands import audit_source, command
from .prices import PRICE_FIELDS, _apply, _new_price, _validate


@dataclass(frozen=True)
class ScenarioPlan:
    rows: tuple
    ingredients: dict
    prices: dict
    new_ingredient_ids: frozenset
    source_purchases: tuple


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Некорректная ссылка на позицию или цену сценария.")


def prepare_scenario(rows):
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 100:
        raise ValidationError("Сценарий должен содержать от 1 до 100 изменений цен.")
    normalized, seen, new_keys, selected_price_ids = [], set(), set(), []
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"ingredient_id", "price_id", "data", "confirm_zero"}:
            raise ValidationError("Недопустимые поля сценария.")
        key = _uuid(row.get("ingredient_id"))
        if key in seen:
            raise ValidationError("Одна позиция может изменяться в сценарии только один раз.")
        seen.add(key)
        has_existing, has_new = "price_id" in row, "data" in row
        if has_existing == has_new:
            raise ValidationError("Выберите сохранённую цену или введите новую — один вариант для каждой позиции.")
        if has_existing:
            if "confirm_zero" in row:
                raise ValidationError("Подтверждение новой нулевой цены не относится к сохранённой записи.")
            price_id = _uuid(row["price_id"])
            normalized.append({"ingredient_id": key, "price_id": price_id})
            selected_price_ids.append(price_id)
        else:
            if not isinstance(row["data"], dict) or set(row["data"]) - set(PRICE_FIELDS):
                raise ValidationError("Недопустимые поля новой цены сценария.")
            confirm_zero = row.get("confirm_zero", False)
            if not isinstance(confirm_zero, bool):
                raise ValidationError("Подтверждение нулевой цены должно быть явной отметкой.")
            normalized.append({"ingredient_id": key, "data": deepcopy(row["data"]), "confirm_zero": confirm_zero})
            new_keys.add(key)
    ingredients = {obj.pk: obj for obj in Ingredient.objects.filter(pk__in=seen, status="active").select_related("current_price__supplier")}
    if len(ingredients) != len(seen):
        raise ValidationError("Позиция больше не доступна. Обновите сценарий.")
    existing_prices = {price.pk: price for price in Price.objects.filter(pk__in=selected_price_ids).select_related("supplier")}
    prices = {}
    for row in normalized:
        ingredient = ingredients[row["ingredient_id"]]
        if "price_id" in row:
            price = existing_prices.get(row["price_id"])
            if price is None or price.ingredient_id != ingredient.pk:
                raise ValidationError("Сохранённая цена не найдена или принадлежит другой позиции.")
            if ingredient.current_price_id == price.pk:
                raise ValidationError("Эта цена уже действует. Уберите позицию из сценария или выберите другую цену.")
            price.ingredient = ingredient
        else:
            price = _validate(Price(ingredient=ingredient), row["data"], row["confirm_zero"])
            # Canonical values preserve exact input precision, defaults and explicit flags.
            row["data"] = {name: getattr(price, name) for name in PRICE_FIELDS}
        prices[ingredient.pk] = price
    purchase_ids = {price.purchase_id for price in prices.values() if price.purchase_id}
    sources = tuple(Purchase.objects.filter(pk__in=purchase_ids).select_related("supplier").prefetch_related("lines__ingredient"))
    for source in sources:
        source.selected_ingredient_ids = frozenset(key for key, price in prices.items() if price.purchase_id == source.id)
    return ScenarioPlan(tuple(normalized), ingredients, prices, frozenset(new_keys), sources)


def apply_scenario(token, rows):
    def execute(group):
        plan = prepare_scenario(rows)
        with audit_source("scenario"):
            for row in plan.rows:
                ingredient = plan.ingredients[row["ingredient_id"]]
                price = plan.prices[ingredient.pk]
                if ingredient.pk in plan.new_ingredient_ids:
                    price = _new_price(group, ingredient, row["data"], row["confirm_zero"])
                _apply(group, ingredient, price)
        return SimpleNamespace(pk=group)
    # Replay is checked before loading referenced records, which may since be deleted.
    return command(token, "scenario-apply", {"rows": rows}, execute)
