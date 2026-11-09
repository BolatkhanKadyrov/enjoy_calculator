import hashlib
import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction

from tseh.calculation import normalized
from tseh.audit_actor import current_actor
from tseh.calculation.recipes import Prep
from tseh.calculation.pricing import effective_thresholds, threshold_state
from tseh.models import AuditEntry, Dish, DishLine, DomainState, GlobalSettings, Ingredient, Operation, Preparation, PreparationLine, Price, PriceApplication, SETTINGS_ID, Supplier
from .queries import catalog, general_settings, proposed_components

SALT = "tseh-command-v1"
_audit_source = ContextVar("tseh_audit_source", default="manual")


@contextmanager
def audit_source(source):
    if source not in dict(AuditEntry.SOURCES):
        raise ValidationError("Неизвестный источник изменения.")
    marker = _audit_source.set(source)
    try:
        yield
    finally:
        _audit_source.reset(marker)


class StaleWrite(ValidationError):
    pass


def stamp(state):
    return signing.dumps({"revision": state.revision, "epoch": str(state.epoch), "operation": str(uuid.uuid4())}, salt=SALT)


def snapshot(obj, fields):
    return json.loads(json.dumps({key: getattr(obj, key) for key in fields}, default=str))


def audit(group, obj, action, before, after, name=None):
    AuditEntry.objects.create(group=group, object_type=obj.__class__.__name__, object_id=str(obj.pk),
        object_name=name or str(obj), action=action, before=before, after=after, source=_audit_source.get(),
        actor_name=current_actor())


def command(token, action, payload, callback):
    try:
        data = signing.loads(token, salt=SALT)
        operation = uuid.UUID(data["operation"])
    except (signing.BadSignature, ValueError, KeyError, TypeError):
        raise ValidationError("Форма недействительна. Откройте её заново.")
    fingerprint = hashlib.sha256(json.dumps([action, payload], sort_keys=True, default=str).encode()).hexdigest()
    with transaction.atomic():
        state = DomainState.objects.select_for_update().get(pk=1)
        if str(state.epoch) != data["epoch"]:
            raise StaleWrite("Данные восстановлены из копии. Откройте форму заново.")
        previous = Operation.objects.filter(pk=operation).first()
        if previous:
            if previous.fingerprint != fingerprint or previous.epoch != state.epoch:
                raise StaleWrite("Это подтверждение уже использовано. Откройте форму заново.")
            return previous.result
        if state.revision != data["revision"]:
            raise StaleWrite("Данные изменились после открытия формы. Обновите страницу и проверьте актуальные значения.")
        result = callback(operation)
        Operation.objects.create(id=operation, epoch=state.epoch, fingerprint=fingerprint, result=result.pk)
        state.revision += 1
        state.save(update_fields=["revision"])
        return result.pk


def save_supplier(token, data, supplier_id=None):
    payload = {**data, "id": supplier_id}
    def apply(group):
        obj = Supplier.objects.get(pk=supplier_id) if supplier_id else Supplier()
        if obj.status != "active":
            raise ValidationError("Поставщик в корзине. Сначала восстановите его.")
        fields = ["name", "note"]
        before = snapshot(obj, fields) if supplier_id else None
        for key in fields:
            setattr(obj, key, data[key])
        obj.full_clean()
        obj.save()
        audit(group, obj, "update" if supplier_id else "create", before, snapshot(obj, fields))
        return obj
    return command(token, "supplier", payload, apply)


def create_ingredient(token, data, *, confirm_zero=False):
    def apply(group):
        validate_active_supplier(data.get("supplier_id"))
        if not isinstance(confirm_zero, bool):
            raise ValidationError("Некорректное подтверждение нулевой цены.")
        # Both foreign keys are deferred until commit; no ingredient can persist without a price.
        price_id = uuid.uuid4()
        obj = Ingredient(name=data["name"], unit=data["unit"], loss=data["loss"], kind=data.get("kind", "ingredient"), current_price_id=price_id)
        obj.full_clean(exclude=["current_price"])
        obj.save(force_insert=True)
        price = Price(id=price_id, ingredient=obj, supplier_id=data.get("supplier_id"), pack_size=data["pack_size"],
                      pack_price=data["pack_price"], price_date=data["price_date"])
        price.full_clean()
        if price.pack_price == Decimal(0) and confirm_zero is not True:
            raise ValidationError("Нулевая цена снизит себестоимость. Подтвердите нулевую цену отдельно.")
        price.save(force_insert=True)
        event = PriceApplication.objects.create(price=price)
        audit(group, obj, "create", None, snapshot(obj, ["name", "kind", "unit", "loss", "current_price_id"]))
        price_snapshot = snapshot(price, ["ingredient_id", "supplier_id", "pack_size", "pack_price", "price_date"])
        price_snapshot.update(ingredient_name=obj.name, supplier_name=price.supplier.name if price.supplier_id else None, unit=obj.unit)
        audit(group, price, "create", None, price_snapshot, obj.name)
        audit(group, event, "apply", None, {"price_id": str(price.pk), "applied_at": event.applied_at.isoformat()}, obj.name)
        return obj
    return command(token, "ingredient-create", {"data": data, "confirm_zero": confirm_zero}, apply)


def update_ingredient(token, ingredient_id, data):
    def apply(group):
        obj = Ingredient.objects.get(pk=ingredient_id)
        if obj.status != "active":
            raise ValidationError("Позиция в корзине. Сначала восстановите её.")
        before = snapshot(obj, ["name", "unit", "loss", "current_price_id"])
        obj.name, obj.loss = data["name"], data["loss"]
        obj.full_clean()
        obj.save(update_fields=["name", "loss"])
        audit(group, obj, "update", before, snapshot(obj, ["name", "unit", "loss", "current_price_id"]))
        return obj
    return command(token, "ingredient-update", {"id": ingredient_id, **data}, apply)


def dish_snapshot(dish):
    result = snapshot(dish, ["name", "base_price", "green_override", "orange_override",
                             "status", "archived_at", "trashed_at", "purge_at"])
    result["lines"] = recipe_snapshot(dish.lines)
    return result


def recipe_snapshot(manager):
    return [{**snapshot(line, ["ingredient_id", "preparation_id", "quantity", "unit", "position"]), "ingredient_name": line.component.name}
            for line in manager.select_related("ingredient", "preparation").all()]


def validate_active_supplier(key):
    if key and not Supplier.objects.filter(pk=key, status="active").exists():
        raise ValidationError("Поставщик недоступен или находится в корзине. Выберите действующего поставщика.")


def validate_lines(lines, preparation_recipe=False):
    if not lines:
        raise ValidationError("Добавьте хотя бы один компонент.")
    if len(lines) > 100:
        raise ValidationError("В одной рецептуре допускается до 100 строк.")
    ingredients = Ingredient.objects.in_bulk([row["ingredient_id"] for row in lines if row.get("ingredient_id")])
    preparations = Preparation.objects.in_bulk([row["preparation_id"] for row in lines if row.get("preparation_id")])
    for row in lines:
        ingredient_id, preparation_id = row.get("ingredient_id"), row.get("preparation_id")
        if bool(ingredient_id) == bool(preparation_id):
            raise ValidationError("В строке выберите один ингредиент или одну заготовку.")
        obj = ingredients.get(ingredient_id) if ingredient_id else preparations.get(preparation_id)
        if obj is None:
            raise ValidationError("Компонент больше не доступен. Обновите форму.")
        if obj.status != "active":
            raise ValidationError("Компонент находится в корзине. Сначала восстановите его.")
        if preparation_recipe and ingredient_id and obj.kind == "packaging":
            raise ValidationError("Упаковка добавляется в блюдо отдельно, а не в заготовку.")
        try:
            normalized(row["quantity"], row["unit"], obj.unit)
        except ValueError as exc:
            raise ValidationError(str(exc))


def validate_dish_thresholds(obj, data, is_new=False):
    old = (obj.green_override, obj.orange_override)
    obj.green_override = data.get("green_override", obj.green_override)
    obj.orange_override = data.get("orange_override", obj.orange_override)
    changed = old != (obj.green_override, obj.orange_override)
    if (is_new or changed) and threshold_state(*effective_thresholds(obj, general_settings())) == "invalid":
        raise ValidationError("Некорректные пороги: зелёный должен быть больше 0 и меньше оранжевого. Проверьте также общие значения.")


def save_dish(token, data, lines, dish_id=None):
    def apply(group):
        validate_lines(lines)
        obj = Dish.objects.get(pk=dish_id) if dish_id else Dish()
        if obj.status == "trashed":
            raise ValidationError("Блюдо находится в корзине. Сначала восстановите его в архив.")
        before = dish_snapshot(obj) if dish_id else None
        obj.name, obj.base_price = data["name"], data["base_price"]
        validate_dish_thresholds(obj, data, is_new=not dish_id)
        obj.full_clean()
        obj.save()
        new_lines = []
        for position, line in enumerate(lines):
            row = DishLine(dish=obj, ingredient_id=line.get("ingredient_id"), preparation_id=line.get("preparation_id"), quantity=line["quantity"], unit=line["unit"], position=position)
            row.full_clean(exclude=["position"], validate_constraints=False)
            new_lines.append(row)
        obj.lines.all().delete()
        DishLine.objects.bulk_create(new_lines)
        audit(group, obj, "update" if dish_id else "create", before, dish_snapshot(obj))
        return obj
    return command(token, "dish", {"id": dish_id, **data, "lines": lines}, apply)


def save_preparation(token, data, lines, preparation_id=None):
    def apply(group):
        validate_lines(lines, preparation_recipe=True)
        obj = Preparation.objects.get(pk=preparation_id) if preparation_id else Preparation()
        if obj.status != "active":
            raise ValidationError("Заготовка в корзине. Сначала восстановите её.")
        before = {**snapshot(obj, ["name", "output_quantity", "output_unit"]), "lines": recipe_snapshot(obj.lines)} if preparation_id else None
        old_unit = obj.unit if preparation_id else None
        obj.name, obj.output_quantity, obj.output_unit = data["name"], data["output_quantity"], data["output_unit"]
        obj.full_clean()
        if preparation_id and old_unit != obj.unit and (obj.dish_uses.exists() or obj.preparation_uses.exists()):
            raise ValidationError("Нельзя менять массу на объём или штуки у используемой заготовки. Сначала исправьте зависимые рецептуры.")
        try:
            catalog({obj.pk: Prep(obj.output_quantity, obj.output_unit, proposed_components(lines))})
        except ValueError as exc:
            raise ValidationError(str(exc))
        obj.save()
        rows = []
        for position, line in enumerate(lines):
            row = PreparationLine(parent=obj, ingredient_id=line.get("ingredient_id"), preparation_id=line.get("preparation_id"), quantity=line["quantity"], unit=line["unit"], position=position)
            row.full_clean(exclude=["position"], validate_constraints=False)
            rows.append(row)
        obj.lines.all().delete()
        PreparationLine.objects.bulk_create(rows)
        after = {**snapshot(obj, ["name", "output_quantity", "output_unit"]), "lines": recipe_snapshot(obj.lines)}
        audit(group, obj, "update" if preparation_id else "create", before, after)
        return obj
    return command(token, "preparation", {"id": preparation_id, **data, "lines": lines}, apply)


def save_thresholds(token, data):
    def apply(group):
        obj, _ = GlobalSettings.objects.get_or_create(pk=SETTINGS_ID)
        before = snapshot(obj, ["green", "orange"])
        obj.green, obj.orange = data["green"], data["orange"]
        obj.full_clean()
        if threshold_state(obj.green, obj.orange) == "invalid":
            raise ValidationError("Общий зелёный порог должен быть меньше оранжевого.")
        obj.save()
        audit(group, obj, "update", before, snapshot(obj, ["green", "orange"]))
        return obj
    return command(token, "thresholds", data, apply)
