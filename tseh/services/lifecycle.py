"""Explicit dish transitions and idempotent scheduled removal of expired trash."""
from datetime import datetime, timedelta
import uuid

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from tseh.models import Dish, DomainState, Ingredient, Preparation, Price, PriceApplication, Purchase, Supplier
from .commands import audit, audit_source, command, dish_snapshot, recipe_snapshot, snapshot

LIFECYCLE_FIELDS = ("status", "archived_at", "trashed_at", "purge_at")
TRANSITIONS = {
    "archive": ("active", "archived"),
    "trash": ("archived", "trashed"),
    "restore": ("trashed", "archived"),
    "activate": ("archived", "active"),
}


def _moment(value):
    value = timezone.now() if value is None else value
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValidationError("Для изменения состояния требуется время с часовым поясом.")
    return value


def lifecycle_snapshot(dish):
    """Deletion retains a readable recipe and lifecycle dates in the journal."""
    return {**dish_snapshot(dish), **snapshot(dish, LIFECYCLE_FIELDS)}


def transition_dish(token, dish_id, action, *, now=None):
    if action not in TRANSITIONS:
        raise ValidationError("Неизвестное действие с блюдом.")
    try:
        dish_id = uuid.UUID(str(dish_id))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Некорректная ссылка на блюдо.")

    def execute(group):
        dish = Dish.objects.filter(pk=dish_id).first()
        if dish is None:
            raise ValidationError("Блюдо больше недоступно. Обновите список.")
        source, target = TRANSITIONS[action]
        if dish.status != source:
            raise ValidationError("Состояние блюда изменилось или действие для него недоступно. Обновите список.")
        moment = _moment(now)
        before = lifecycle_snapshot(dish)
        dish.status = target
        if target == "archived":
            dish.archived_at = moment
        if target == "trashed":
            dish.trashed_at = moment
            dish.purge_at = moment + timedelta(days=30)
        else:
            dish.purge_at = None
        # archived_at and trashed_at remain historical after activation; every
        # transition is additionally preserved in the immutable journal.
        dish.full_clean()
        dish.save(update_fields=LIFECYCLE_FIELDS)
        audit(group, dish, action, before, lifecycle_snapshot(dish))
        return dish

    return command(token, "dish-lifecycle", {"dish_id": dish_id, "action": action}, execute)


def purge_due_dishes(*, now=None):
    """Remove only due trashed dishes under the same lock as every domain write.

    Empty runs do not increment revision or create journal entries. A failure
    rolls back the entire batch. No job receipt is needed: removed dishes no
    longer match the query, making retries idempotent.
    """
    with transaction.atomic():
        state = DomainState.objects.select_for_update().get(pk=1)
        moment = _moment(now)
        dishes = list(Dish.objects.filter(status="trashed", purge_at__lte=moment).order_by("purge_at", "id"))
        if not dishes:
            return 0
        group = uuid.uuid4()
        with audit_source("cleanup"):
            for dish in dishes:
                audit(group, dish, "purge", lifecycle_snapshot(dish), None)
                dish.delete()
        state.revision += 1
        state.save(update_fields=["revision"])
        return len(dishes)


CATALOG_MODELS = {"supplier": Supplier, "preparation": Preparation, "ingredient": Ingredient}


def _catalog_object(kind, key):
    model = CATALOG_MODELS.get(kind)
    if model is None:
        raise ValidationError("Неизвестный раздел справочника.")
    try:
        key = uuid.UUID(str(key))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Некорректная ссылка на запись справочника.")
    obj = model.objects.filter(pk=key).first()
    if obj is None:
        raise ValidationError("Запись больше недоступна. Обновите список.")
    return obj


def catalog_dependencies(kind, key):
    """Human-readable direct references, including archive and trash recipes."""
    obj = _catalog_object(kind, key)
    dependencies = []
    if kind == "supplier":
        names = Price.objects.filter(supplier=obj).order_by("ingredient__name").values_list("ingredient__name", flat=True).distinct()
        dependencies.extend(f"Цены позиции: {name}" for name in names)
        dependencies.extend(f"Закупка от {date:%d.%m.%Y}" for date in
            Purchase.objects.filter(supplier=obj).values_list("purchase_date", flat=True))
    else:
        lookup = {f"lines__{kind}": obj}
        for dish in Dish.objects.filter(**lookup).distinct():
            dependencies.append(f"Блюдо: {dish.name} ({dish.get_status_display()})")
        dependencies.extend(f"Заготовка: {prep.name}" for prep in Preparation.objects.filter(**lookup).distinct())
        if kind == "ingredient":
            dependencies.extend(f"Закупка от {date:%d.%m.%Y}" for date in
                Purchase.objects.filter(Q(lines__ingredient=obj) | Q(prices__ingredient=obj)).distinct()
                .values_list("purchase_date", flat=True))
    return dependencies


def catalog_snapshot(obj, kind, *, with_prices=False):
    if kind == "preparation":
        result = {**snapshot(obj, ["name", "output_quantity", "output_unit"]),
                  "lines": recipe_snapshot(obj.lines)}
    elif kind == "supplier":
        result = snapshot(obj, ["name", "note"])
    else:
        result = snapshot(obj, ["name", "kind", "unit", "loss", "current_price_id"])
        if with_prices:
            result["prices"] = [snapshot(price, [field.attname for field in Price._meta.concrete_fields])
                for price in Price.objects.filter(ingredient=obj).order_by("id")]
            result["applications"] = [snapshot(event, ["id", "price_id", "applied_at"])
                for event in PriceApplication.objects.filter(price__ingredient=obj).order_by("applied_at", "id")]
    return {**result, **snapshot(obj, ["status", "trashed_at"])}


def _purge_unused_ingredient(obj):
    """Delete the explicitly confirmed aggregate, not any external dependent.

    Its own prices and application history are internal data preserved in the
    purge audit. Ingredient/current_price form a deferred FK cycle, so ORM's
    eager PROTECT collector cannot perform this authorized aggregate removal.
    All SQL identifiers come from our models; the object id is a bound value.
    """
    quote = connection.ops.quote_name
    prices = quote(Price._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {quote(PriceApplication._meta.db_table)} WHERE price_id IN "
                       f"(SELECT id FROM {prices} WHERE ingredient_id = %s)", [obj.pk])
        cursor.execute(f"DELETE FROM {prices} WHERE ingredient_id = %s", [obj.pk])
        cursor.execute(f"DELETE FROM {quote(Ingredient._meta.db_table)} WHERE id = %s", [obj.pk])


def transition_catalog(token, kind, key, action, *, now=None):
    """Catalog trash has no automatic deadline; permanent removal is explicit."""
    try:
        key = uuid.UUID(str(key))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Некорректная ссылка на запись справочника.")
    if kind not in CATALOG_MODELS:
        raise ValidationError("Неизвестный раздел справочника.")
    if action not in {"trash", "restore", "purge"}:
        raise ValidationError("Неизвестное действие с записью справочника.")

    def execute(group):
        obj = _catalog_object(kind, key)
        expected = "active" if action == "trash" else "trashed"
        if obj.status != expected:
            raise ValidationError("Состояние записи изменилось или действие недоступно. Обновите список.")
        if action != "restore":
            dependencies = catalog_dependencies(kind, key)
            if dependencies:
                raise ValidationError(["Запись используется и не может быть перемещена в корзину или удалена.", *dependencies])
        before = catalog_snapshot(obj, kind, with_prices=action == "purge")
        if action != "purge":
            obj.status = "trashed" if action == "trash" else "active"
            if action == "trash":
                obj.trashed_at = _moment(now)
            obj.full_clean()
            obj.save(update_fields=["status", "trashed_at"])
            audit(group, obj, action, before, catalog_snapshot(obj, kind))
            return obj
        audit(group, obj, "purge", before, None)
        try:
            if kind == "ingredient":
                _purge_unused_ingredient(obj)
            else:
                obj.delete()
        except ProtectedError:
            raise ValidationError("Появились связанные записи. Обновите список зависимостей перед удалением.")
        # Django clears obj.pk after deletion; command() stores the stable result.
        obj.pk = key
        return obj

    return command(token, "catalog-lifecycle", {"kind": kind, "id": key, "action": action}, execute)


def delete_catalog_object(token, kind, key):
    """Compatibility wrapper; direct removal of an active entry is forbidden."""
    return transition_catalog(token, kind, key, "purge")
