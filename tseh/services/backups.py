"""Versioned domain snapshots and guarded, atomic replacement.

No account, session, secret, command receipt or restore-staging data crosses the
backup boundary. Validation builds a complete in-memory graph without touching
live tables. Only the final explicitly confirmed operation replaces domain data.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
import re
import uuid

from django.core.exceptions import ValidationError
from django.core import signing
from django.db import connection, models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from tseh.calculation import normalized
from tseh.audit_actor import current_actor
from tseh.calculation.purchases import aggregate_lines
from tseh.calculation.recipes import Component, Item, Prep, RecipeGraph
from tseh.models import (AuditEntry, CatalogDraft, Dish, DishLine, DomainState, GlobalSettings,
    Ingredient, Operation, Preparation, PreparationLine, Price, PriceApplication,
    Purchase, PurchaseLine, SETTINGS_ID, Supplier, UnallocatedDelivery)
from tseh.models.transfer import RestoreSession
from tseh.catalog_draft_payload import text_value, validate_draft_payload

FORMAT = "tseh-backup"
SCHEMA_VERSION = 2
MAX_BACKUP_BYTES = 25 * 1024 * 1024
MAX_RECORDS = 50_000
MAX_JSON_DEPTH = 32
RESTORE_LIFETIME = timedelta(hours=24)
REVIEW_SALT = "tseh-restore-review-v1"
TABLES = {
    "settings": GlobalSettings, "suppliers": Supplier, "ingredients": Ingredient,
    "prices": Price, "price_applications": PriceApplication, "purchases": Purchase,
    "purchase_lines": PurchaseLine, "unallocated_delivery": UnallocatedDelivery,
    "preparations": Preparation, "preparation_lines": PreparationLine,
    "dishes": Dish, "dish_lines": DishLine, "catalog_drafts": CatalogDraft, "journal": AuditEntry,
}
LEGACY_TABLES = set(TABLES) - {"catalog_drafts"}
DECIMAL_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
SAFE_AUDIT_TYPES = {model.__name__ for model in TABLES.values()} | {"DomainState"}
FORBIDDEN_KEYS = {"password", "password_hash", "secret", "secret_key", "token", "session_key",
                  "authorization", "csrfmiddlewaretoken", "cookie", "credentials"}


def _fail(message):
    raise ValidationError(message)


def _now(value=None):
    value = timezone.now() if value is None else value
    if not isinstance(value, datetime) or timezone.is_naive(value):
        _fail("Время снимка должно содержать часовой пояс.")
    try:
        return value.astimezone(dt_timezone.utc)
    except (OverflowError, ValueError):
        _fail("Время снимка выходит за допустимый диапазон UTC.")


def _json_value(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(dt_timezone.utc).isoformat()
    if isinstance(value, (date, uuid.UUID)):
        return str(value)
    return deepcopy(value)


def encode_backup(document):
    # PostgreSQL JSONB changes object-key order, so receipts hash a canonical
    # encoding while preserving the meaningful order of record arrays.
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def data_summary(document):
    return {name: len(rows) for name, rows in document["data"].items()}


def _microseconds(delta):
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _export_locked(now):
    data = {}
    for name, model in TABLES.items():
        fields = [field.attname for field in model._meta.concrete_fields]
        rows = []
        for obj in model.objects.order_by("pk").iterator():
            row = {name: _json_value(getattr(obj, name)) for name in fields}
            if model is Dish:
                row["remaining_microseconds"] = (max(0, _microseconds(obj.purge_at - now))
                    if obj.status == "trashed" else None)
            rows.append(row)
        data[name] = rows
    return {"format": FORMAT, "schema_version": SCHEMA_VERSION, "snapshot_at": now.isoformat(), "data": data}


def export_snapshot(*, now=None):
    with transaction.atomic():
        DomainState.objects.select_for_update().get(pk=1)
        document = _export_locked(_now(now))
        validate_backup(document)
    return document


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        try:
            key.encode("utf-8")
        except UnicodeEncodeError:
            _fail("JSON содержит некорректный символ Unicode в имени поля.")
        if key in result:
            _fail("В JSON повторяется ключ: " + key[:80])
        result[key] = value
    return result


def _shape(value, depth=0, counter=None):
    counter = [0] if counter is None else counter
    counter[0] += 1
    if depth > MAX_JSON_DEPTH or counter[0] > 2_000_000:
        _fail("JSON слишком сложный: превышен предел вложенности или числа значений.")
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and len(value) > MAX_BACKUP_BYTES:
            _fail("Слишком длинное текстовое значение в JSON.")
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                _fail("JSON содержит некорректный символ Unicode.")
        return
    if isinstance(value, list):
        for item in value:
            _shape(item, depth + 1, counter)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 100:
                _fail("Некорректное имя поля JSON.")
            _shape(key, depth + 1, counter)
            if key.casefold() in FORBIDDEN_KEYS:
                _fail("Копия содержит поля авторизации или секретов. Они не входят в доменные данные.")
            _shape(item, depth + 1, counter)
        return
    _fail("JSON должен содержать точные значения: десятичные числа сохраняются строками.")


def _read(value):
    if isinstance(value, bytes):
        if len(value) > MAX_BACKUP_BYTES:
            _fail("JSON превышает допустимый размер 25 МиБ.")
        try:
            value = value.decode("utf-8-sig")
        except UnicodeDecodeError:
            _fail("Нужен JSON-файл в кодировке UTF-8.")
    if isinstance(value, str):
        try:
            byte_size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            _fail("JSON содержит некорректный символ Unicode.")
        if byte_size > MAX_BACKUP_BYTES:
            _fail("JSON превышает допустимый размер 25 МиБ.")
        try:
            value = json.loads(value, object_pairs_hook=_object_pairs,
                parse_constant=lambda _: _fail("NaN и Infinity недопустимы в JSON."))
        except (ValueError, RecursionError, OverflowError):
            _fail("Не удалось прочитать JSON. Выберите полный файл экспорта Цеха.")
    if not isinstance(value, dict):
        _fail("Нужен объект полного JSON-экспорта Цеха.")
    _shape(value)
    if len(encode_backup(value)) > MAX_BACKUP_BYTES:
        _fail("JSON превышает допустимый размер 25 МиБ.")
    return deepcopy(value)


def _date(value, *, timestamp=False):
    if not isinstance(value, str):
        _fail("Дата должна быть строкой ISO 8601.")
    try:
        parsed = parse_datetime(value) if timestamp else parse_date(value)
    except (ValueError, TypeError):
        parsed = None
    if parsed is None or (timestamp and timezone.is_naive(parsed)):
        _fail("Некорректная дата или временная отметка без часового пояса.")
    if timestamp:
        try:
            parsed = parsed.astimezone(dt_timezone.utc)
        except (OverflowError, ValueError):
            _fail("Временная отметка выходит за допустимый диапазон UTC.")
    return parsed


def _field_value(field, value):
    if value is None:
        if field.null:
            return None
        _fail(f"Поле {field.name} не может быть пустым.")
    if isinstance(field, models.ForeignKey):
        return _field_value(field.target_field, value)
    if isinstance(field, models.UUIDField):
        if not isinstance(value, str):
            _fail("Идентификатор UUID должен быть строкой.")
        try:
            return uuid.UUID(value)
        except (ValueError, AttributeError):
            _fail("Некорректный UUID в копии.")
    if isinstance(field, models.BooleanField):
        if not isinstance(value, bool):
            _fail("Логическое поле должно быть true или false.")
        return value
    if isinstance(field, models.DecimalField):
        if not isinstance(value, str) or len(value) > 100 or not DECIMAL_RE.fullmatch(value):
            _fail("Десятичные значения должны быть точными строками без разделителей и экспоненты.")
        try:
            result = Decimal(value)
            field.run_validators(result)
        except (InvalidOperation, ValidationError):
            _fail(f"Значение {field.name} выходит за допустимый диапазон или точность.")
        return result
    if isinstance(field, models.DateTimeField):
        return _date(value, timestamp=True)
    if isinstance(field, models.DateField):
        return _date(value)
    if isinstance(field, models.IntegerField):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 9223372036854775806:
            _fail("Некорректное целое число в копии.")
        if field.primary_key and value < 1:
            _fail("Числовой идентификатор должен быть положительным.")
        field.run_validators(value)
        return value
    if isinstance(field, models.JSONField):
        if not isinstance(value, dict):
            _fail("Снимок журнала должен быть объектом или null.")
        return deepcopy(value)
    if isinstance(field, (models.CharField, models.TextField)):
        if not isinstance(value, str) or "\x00" in value:
            _fail("Некорректное текстовое поле.")
        field.run_validators(value)
        field.validate(value, None)
        return value
    _fail("Неподдерживаемый тип поля схемы JSON.")


def _rows(document):
    tables = {}
    for name, model in TABLES.items():
        values = document["data"][name]
        if not isinstance(values, list):
            _fail(f"Раздел {name} должен быть списком.")
        fields = {field.attname: field for field in model._meta.concrete_fields}
        keys = set(fields) | ({"remaining_microseconds"} if model is Dish else set())
        found = {}
        for row in values:
            # Old v1 copies have no named author; never invent one.
            if model is AuditEntry and isinstance(row, dict) and set(row) == keys - {"actor_name"}:
                row["actor_name"] = ""
            if not isinstance(row, dict) or set(row) != keys:
                _fail(f"Неполный или неизвестный набор полей в разделе {name}.")
            obj = model(**{key: _field_value(field, row[key]) for key, field in fields.items()})
            if obj.pk in found:
                _fail(f"Повторный идентификатор в разделе {name}.")
            if model is Dish:
                obj._remaining_microseconds = row["remaining_microseconds"]
            found[obj.pk] = obj
        tables[name] = found
    return tables


def _references(tables):
    by_model = {model: tables[name] for name, model in TABLES.items()}
    for name, model in TABLES.items():
        for obj in tables[name].values():
            for field in model._meta.concrete_fields:
                if not isinstance(field, models.ForeignKey):
                    continue
                key = getattr(obj, field.attname)
                if key is not None and key not in by_model[field.related_model]:
                    _fail(f"Нарушена ссылка {model.__name__}.{field.name}.")
    for obj in tables["ingredients"].values():
        if tables["prices"][obj.current_price_id].ingredient_id != obj.pk:
            _fail("Действующая цена принадлежит другой позиции.")
    events = defaultdict(list)
    for event in tables["price_applications"].values():
        events[tables["prices"][event.price_id].ingredient_id].append(event)
    for obj in tables["ingredients"].values():
        if not events[obj.pk] or not any(event.price_id == obj.current_price_id for event in events[obj.pk]):
            _fail("У действующей цены отсутствует событие применения.")
        # Deleting a current offer can remove later events; the remaining final
        # event still determines the explicitly selected current price.
        last = max(events[obj.pk], key=lambda event: (event.applied_at, event.id))
        if last.price_id != obj.current_price_id:
            _fail("Действующая цена не соответствует последнему событию применения.")
    trashed_suppliers = {row.pk for row in tables["suppliers"].values() if row.status == "trashed"}
    if any(row.supplier_id in trashed_suppliers for name in ("prices", "purchases") for row in tables[name].values()):
        _fail("Поставщик в корзине не может иметь связанные цены или закупки.")
    trashed_ingredients = {row.pk for row in tables["ingredients"].values() if row.status == "trashed"}
    if any(row.ingredient_id in trashed_ingredients for name in ("dish_lines", "preparation_lines", "purchase_lines")
           for row in tables[name].values()):
        _fail("Позиция в корзине не может использоваться в рецептурах или закупках.")
    trashed_preparations = {row.pk for row in tables["preparations"].values() if row.status == "trashed"}
    if any(row.preparation_id in trashed_preparations for name in ("dish_lines", "preparation_lines")
           for row in tables[name].values()):
        _fail("Заготовка в корзине не может использоваться в других рецептурах.")


def _positive(value, label, *, zero=False, limit=10**12):
    if value < 0 or (not zero and value == 0) or value >= limit:
        _fail(f"{label}: значение вне допустимого диапазона.")


def _validate_objects(tables, snapshot_at):
    for row in tables["catalog_drafts"].values():
        _validate_draft_object(row)
    from .catalog_drafts import validate_draft_links
    validate_draft_links(tables["catalog_drafts"].values())
    if len(tables["settings"]) > 1 or (tables["settings"] and SETTINGS_ID not in tables["settings"]):
        _fail("Некорректная единственная запись общих настроек.")
    for row in tables["settings"].values():
        for value in (row.green, row.orange):
            if value is not None and value <= 0:
                _fail("Порог должен быть положительным.")
        if row.green is not None and row.orange is not None and row.green >= row.orange:
            _fail("Некорректная общая пара порогов.")
    for row in tables["ingredients"].values():
        if not 0 <= row.loss < 100 or (row.kind == "packaging" and row.loss != 0):
            _fail("Некорректные потери сырья или упаковки.")
    for name in ("suppliers", "ingredients", "preparations"):
        for row in tables[name].values():
            if row.status == "trashed" and row.trashed_at is None:
                _fail("У записи справочника в корзине отсутствует дата помещения.")
    for row in tables["prices"].values():
        if row.pack_size is not None:
            _positive(row.pack_size, "Фасовка")
        if row.pack_price is not None:
            _positive(row.pack_price, "Цена упаковки", zero=True)
        if not row.purchase_id:
            if (row.pack_size is None or row.pack_price is None or row.goods_total is not None
                    or row.quantity_total is not None or row.delivery_total != 0):
                _fail("Обычная цена содержит некорректные данные закупки.")
        elif row.goods_total is None or row.quantity_total is None or row.goods_total < 0 or row.quantity_total <= 0 or row.delivery_total < 0:
            _fail("Некорректные итоговые значения цены из закупки.")
    for row in tables["unallocated_delivery"].values():
        _positive(row.amount, "Нераспределённая доставка", zero=True)
    for row in tables["dishes"].values():
        _positive(row.base_price, "Отпускная цена", zero=True)
        for value in (row.green_override, row.orange_override):
            if value is not None and value <= 0:
                _fail("Индивидуальный порог должен быть положительным.")
        # A valid inherited conflict (e.g. own G=55 and inherited O=50) is
        # intentionally preserved. No blanket effective-pair rejection here.
        if row.green_override is not None and row.orange_override is not None and row.green_override >= row.orange_override:
            _fail("Некорректная явно заданная пара порогов блюда.")
        if row.trashed_at is not None and row.archived_at is None:
            _fail("У даты корзины отсутствует дата архивации.")
        if row.status == "trashed":
            remaining = row._remaining_microseconds
            if any(value is None for value in (row.archived_at, row.trashed_at, row.purge_at)):
                _fail("У блюда в корзине отсутствуют временные отметки.")
            if not isinstance(remaining, int) or isinstance(remaining, bool) or not 0 <= remaining <= 30 * 86400 * 1_000_000:
                _fail("Некорректный остаток срока корзины.")
            if remaining != max(0, _microseconds(row.purge_at - snapshot_at)):
                _fail("Остаток срока корзины не соответствует времени снимка.")
        elif row.purge_at is not None or row._remaining_microseconds is not None:
            _fail("Срок окончательного удаления допустим только для корзины.")
        elif row.status == "archived" and row.archived_at is None:
            _fail("У архивного блюда отсутствует дата архивации.")


def _recipe_graph(tables):
    grouped = {}
    for name, parent_field, parents in (("dish_lines", "dish_id", "dishes"),
                                        ("preparation_lines", "parent_id", "preparations")):
        grouped[name] = defaultdict(list)
        seen = set()
        for row in tables[name].values():
            parent = getattr(row, parent_field)
            if (parent, row.position) in seen:
                _fail("Повторный порядок строки рецептуры.")
            seen.add((parent, row.position))
            if bool(row.ingredient_id) == bool(row.preparation_id):
                _fail("У строки рецептуры должен быть ровно один компонент.")
            grouped[name][parent].append(row)
        for parent in tables[parents]:
            rows = grouped[name][parent]
            if not 1 <= len(rows) <= 100:
                _fail("Состав блюда или заготовки должен содержать от 1 до 100 строк.")
            rows.sort(key=lambda row: (row.position, row.pk))
    def components(rows):
        return tuple(Component("ingredient" if row.ingredient_id else "preparation",
            row.ingredient_id or row.preparation_id, row.quantity, row.unit) for row in rows)
    items = {row.pk: Item(row.unit, row.loss, None, None, row.kind,
        tables["prices"][row.current_price_id].effective_unit_price) for row in tables["ingredients"].values()}
    preps = {row.pk: Prep(row.output_quantity, row.output_unit, components(grouped["preparation_lines"][row.pk]))
             for row in tables["preparations"].values()}
    try:
        graph = RecipeGraph(items, preps)
        for dish in tables["dishes"]:
            graph.calculate(components(grouped["dish_lines"][dish]))
    except (ValueError, KeyError, ZeroDivisionError) as exc:
        _fail("Некорректная рецептура: " + str(exc))


def _validate_purchases(tables):
    grouped, unique, linked = defaultdict(list), set(), {}
    for row in tables["purchase_lines"].values():
        _positive(row.pack_size, "Фасовка")
        _positive(row.pack_price, "Цена упаковки", zero=True)
        if row.pack_count is not None:
            _positive(row.pack_count, "Количество упаковок")
        _positive(row.delivery_share, "Доля доставки", zero=True)
        if (row.purchase_id, row.position) in unique:
            _fail("Повторный порядок строки закупки.")
        unique.add((row.purchase_id, row.position))
        grouped[row.purchase_id].append(row)
    for row in tables["prices"].values():
        if row.purchase_id:
            pair = (row.purchase_id, row.ingredient_id)
            if pair in linked:
                _fail("В закупке повторяется связанная запись цены позиции.")
            linked[pair] = row
    for purchase in tables["purchases"].values():
        rows = grouped[purchase.pk]
        if not 1 <= len(rows) <= 100:
            _fail("Закупка должна содержать от 1 до 100 строк.")
        _positive(purchase.delivery_total, "Доставка", zero=True)
        if sum((Fraction(row.delivery_share) for row in rows), Fraction()) != purchase.delivery_total:
            _fail("Сумма долей доставки не совпадает с доставкой закупки.")
        if purchase.delivery_total and (any(row.pack_count is None for row in rows)
                or sum((row.goods_total for row in rows), Fraction()) <= 0):
            _fail("Для платной доставки нужны полный объём и положительная стоимость товаров.")
        try:
            amounts = aggregate_lines(rows)
        except ValueError as exc:
            _fail(str(exc))
        for key, values in amounts.items():
            price = linked.pop((purchase.pk, key), None)
            if price is None:
                _fail("У строки закупки отсутствует связанная цена.")
            source = [row for row in rows if row.ingredient_id == key]
            if any(getattr(price, field) != values[field] for field in ("goods_total", "quantity_total", "delivery_total")):
                _fail("Итоги цены не соответствуют исходным строкам закупки.")
            if price.supplier_id != purchase.supplier_id or price.price_date != purchase.purchase_date:
                _fail("Источник цены не соответствует закупке.")
            expected_pack = (source[0].pack_size, source[0].pack_price) if len(source) == 1 else (None, None)
            expected_comment = source[0].comment if len(source) == 1 else purchase.comment
            if ((price.pack_size, price.pack_price) != expected_pack
                    or price.is_one_off != any(row.is_one_off for row in source) or price.comment != expected_comment):
                _fail("Фасовка или разовая отметка цены не соответствует строкам закупки.")
    if linked:
        _fail("Цена ссылается на отсутствующую позицию в закупке.")


def _validate_draft_object(row):
    text_value(row.name, 160, "Название черновика", required=True)
    text_value(row.source_text, 20000, "Исходный текст")
    text_value(row.notes, 8000, "Заметки")
    if row.source not in {"manual", "import"} or type(row.version) is not int or row.version < 1:
        _fail("Некорректные источник или версия черновика.")
    if row.payload != validate_draft_payload(row.kind, row.payload):
        _fail("В полном черновике отсутствуют поля данных.")
    if (row.published_object_id is None) != (row.published_at is None):
        _fail("Некорректная квитанция публикации черновика.")
    if row.updated_at < row.created_at or (row.published_at is not None and
            not row.created_at <= row.published_at <= row.updated_at):
        _fail("Некорректная последовательность дат черновика.")
    # Publication is a historical receipt, not a live FK: the target may have
    # been purged after publication without invalidating the original source.


def _validate_draft_snapshot(snap):
    fields = {field.attname: field for field in CatalogDraft._meta.concrete_fields if field.name != "id"}
    if not isinstance(snap, dict) or set(snap) != set(fields):
        _fail("Неполный или неизвестный исторический снимок черновика.")
    row = CatalogDraft(**{name: _field_value(field, snap[name]) for name, field in fields.items()})
    _validate_draft_object(row)


def _validate_journal(tables):
    for entry in tables["journal"].values():
        if entry.object_type == "CatalogDraft":
            if entry.action not in {"draft_create", "draft_update", "draft_delete", "draft_publish"} or entry.source not in {"manual", "import"}:
                _fail("Некорректное действие или источник журнала черновиков.")
            try:
                uuid.UUID(entry.object_id)
            except ValueError:
                _fail("Некорректный исторический UUID черновика.")
            if entry.before is None and entry.after is None:
                _fail("В записи черновика отсутствуют оба снимка.")
            for snap in (entry.before, entry.after):
                if snap is not None:
                    _validate_draft_snapshot(snap)
            continue
        if (entry.object_type not in SAFE_AUDIT_TYPES or not entry.object_id
                or entry.action not in {"create", "update", "delete", "apply", "archive", "trash", "restore", "activate", "purge", "transfer"}):
            _fail("Некорректный тип объекта или действие журнала.")
        if entry.before is None and entry.after is None:
            _fail("В записи журнала отсутствуют оба снимка.")
        if entry.object_type in {"AuditEntry", "DomainState"}:
            if entry.object_type != "DomainState" or entry.action != "restore" or entry.source != "import":
                _fail("Недопустимая служебная запись журнала.")
        elif entry.object_type in {"PriceApplication", "DishLine", "PreparationLine"}:
            if not entry.object_id.isdigit() or int(entry.object_id) < 1:
                _fail("Некорректный исторический идентификатор журнала.")
        else:
            try:
                uuid.UUID(entry.object_id)
            except ValueError:
                _fail("Некорректный исторический UUID журнала.")
        # Snapshots deliberately may refer to deleted records. Shape, field
        # types and identifiers remain checked without requiring live links.
        for snap in (entry.before, entry.after):
            if snap is not None:
                _validate_snapshot(snap)


def _validate_snapshot(snap):
    allowed = {field.attname for model in TABLES.values() if model not in {AuditEntry, CatalogDraft} for field in model._meta.concrete_fields} | {
        "lines", "applications", "prices", "price", "ingredient_name", "supplier_name", "reason", "delivery_action",
        "removed_delivery", "kept_delivery", "source_purchase", "previous_delivery", "transferred_amount",
        "snapshot_at", "restored_at", "source_hash", "counts", "trash_deadlines", "dish_id",
        "historical_trashed_at", "previous_purge_at", "remaining_microseconds",
    }
    if not snap or set(snap) - allowed:
        _fail("Неизвестные или отсутствующие поля исторического снимка.")
    nullable = {field.attname for model in TABLES.values() if model not in {AuditEntry, CatalogDraft}
                for field in model._meta.concrete_fields if field.null} | {"supplier_name"}
    for key, value in snap.items():
        if value is None:
            if key not in nullable:
                _fail("Обязательное поле исторического снимка не может быть null.")
            continue
        if key == "id":
            if type(value) is int and value > 0:
                continue
            if not isinstance(value, str):
                _fail("Некорректный идентификатор исторического снимка.")
            try:
                uuid.UUID(value)
            except ValueError:
                _fail("Некорректный UUID исторического снимка.")
            continue
        if key in {"lines", "applications", "prices", "trash_deadlines"}:
            if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
                _fail("Повреждён состав исторического снимка.")
            for row in value:
                _validate_snapshot(row)
        elif key == "price":
            if not isinstance(value, dict):
                _fail("Повреждён снимок исторической цены.")
            _validate_snapshot(value)
        elif key.endswith("_id") or key == "source_purchase":
            if not isinstance(value, str):
                _fail("Историческая ссылка должна быть строкой UUID.")
            try:
                uuid.UUID(value)
            except ValueError:
                _fail("Некорректная ссылка исторического снимка.")
        elif key in {"quantity", "pack_size", "pack_price", "loss", "base_price", "output_quantity",
                      "green", "orange", "green_override", "orange_override", "goods_total", "quantity_total",
                      "delivery_total", "delivery_share", "amount", "pack_count", "previous_delivery",
                      "transferred_amount", "removed_delivery", "kept_delivery"}:
            if not isinstance(value, str) or len(value) > 100:
                _fail("Некорректное десятичное значение исторического снимка.")
            try:
                number = Decimal(value)
            except InvalidOperation:
                _fail("Повреждено число исторического снимка.")
            if not number.is_finite() or number < 0:
                _fail("Некорректное число исторического снимка.")
        elif key in {"created_at", "applied_at", "archived_at", "trashed_at", "purge_at", "restored_at", "snapshot_at",
                     "historical_trashed_at", "previous_purge_at"}:
            _date(value, timestamp=True)
        elif key in {"price_date", "purchase_date"}:
            _date(value)
        elif key in {"unit", "output_unit"} and (not isinstance(value, str) or value not in {"g", "kg", "ml", "l", "pc"}):
            _fail("Некорректная единица исторического снимка.")
        elif key == "status" and (not isinstance(value, str) or value not in {"active", "archived", "trashed"}):
            _fail("Некорректное состояние исторического блюда.")
        elif key == "kind" and (not isinstance(value, str) or value not in {"ingredient", "packaging"}):
            _fail("Некорректный вид позиции исторического снимка.")
        elif key == "delivery_action" and (not isinstance(value, str) or value not in {"reallocate", "drop_share", "keep", "delete", "remove_all"}):
            _fail("Некорректное действие с доставкой в историческом снимке.")
        elif key == "source_hash" and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)):
            _fail("Некорректный отпечаток файла исторического восстановления.")
        elif key == "counts" and (not isinstance(value, dict) or set(value) not in (set(TABLES), LEGACY_TABLES)
                or any(type(number) is not int or number < 0 for number in value.values())):
            _fail("Некорректные счётчики исторического восстановления.")
        elif key in {"position", "remaining_microseconds"} and (type(value) is not int or value < 0):
            _fail("Некорректное целое число исторического снимка.")
        elif key == "is_one_off" and type(value) is not bool:
            _fail("Некорректная разовая отметка исторического снимка.")
        elif key in {"name", "note", "ingredient_name", "supplier_name", "reason", "comment"} and not isinstance(value, str):
            _fail("Некорректный текст исторического снимка.")


def validate_backup(value):
    document = _read(value)
    if set(document) != {"format", "schema_version", "snapshot_at", "data"} or document["format"] != FORMAT:
        _fail("Файл не является полным экспортом Цеха.")
    if type(document["schema_version"]) is not int or document["schema_version"] not in {1, SCHEMA_VERSION}:
        _fail("Версия схемы JSON не поддерживается.")
    snapshot_at = _date(document["snapshot_at"], timestamp=True)
    expected_tables = LEGACY_TABLES if document["schema_version"] == 1 else set(TABLES)
    if not isinstance(document["data"], dict) or set(document["data"]) != expected_tables:
        _fail("В JSON отсутствуют обязательные разделы или присутствуют неизвестные данные.")
    if document["schema_version"] == 1:
        document["schema_version"] = SCHEMA_VERSION
        document["data"]["catalog_drafts"] = []
    if any(not isinstance(rows, list) for rows in document["data"].values()):
        _fail("Разделы данных должны быть списками.")
    if sum(len(rows) for rows in document["data"].values()) > MAX_RECORDS:
        _fail("JSON содержит больше 50 000 записей.")
    tables = _rows(document)
    _references(tables)
    _validate_objects(tables, snapshot_at)
    _validate_purchases(tables)
    _recipe_graph(tables)
    _validate_journal(tables)
    return document


def _session(session_id, user, *, lock=False):
    try:
        key = uuid.UUID(str(session_id))
    except (ValueError, TypeError, AttributeError):
        _fail("Подготовка восстановления не найдена. Загрузите файл заново.")
    query = RestoreSession.objects.select_for_update() if lock else RestoreSession.objects
    obj = query.filter(pk=key, owner_id=user.pk).first()
    if obj is None or obj.expires_at <= timezone.now():
        _fail("Подготовка восстановления недоступна или истекла. Загрузите файл заново.")
    if obj.completed_at is None and any(
            not isinstance(document, dict) or type(document.get("schema_version")) is not int
            or document["schema_version"] != SCHEMA_VERSION
            for document in (obj.proposed, obj.previous)):
        # Old files remain importable, but a prepared confirmation must retain
        # its original hashes. Never upgrade the reviewed documents silently.
        _fail("Подготовка создана в предыдущей версии. Загрузите исходный JSON заново.")
    return obj


def get_restore(session_id, user):
    return _session(session_id, user)


def _review_identity(session, user):
    return {"session": str(session.pk), "user": str(user.pk), "source_hash": session.source_hash,
        "epoch": str(session.baseline_epoch), "revision": session.baseline_revision,
        "previous_hash": hashlib.sha256(encode_backup(session.previous)).hexdigest()}


def restore_token(session, user):
    """Bind a displayed confirmation to exactly the reviewed pre-import copy.

    A shared account may have several tabs. Refreshing one RestoreSession must
    not make the old confirmation form in another tab authorize its new copy.
    Downloading the reviewed file intentionally leaves this token valid.
    """
    if session.owner_id != user.pk:
        _fail("Эта подготовка восстановления недоступна.")
    return signing.dumps(_review_identity(session, user), salt=REVIEW_SALT, compress=True)


def _check_review_token(session, user, review_token):
    try:
        if not isinstance(review_token, str) or len(review_token) > 4096:
            raise signing.BadSignature("Invalid review token")
        payload = signing.loads(review_token, salt=REVIEW_SALT, max_age=int(RESTORE_LIFETIME.total_seconds()))
        if payload == _review_identity(session, user):
            return
    except (signing.BadSignature, ValueError, TypeError):
        pass
    _fail("Подготовленная копия или подтверждение изменились. Откройте актуальную проверку восстановления, скачайте её копию и подтвердите замену заново.")


def prepare_restore(user, value):
    document = validate_backup(value)
    with transaction.atomic():
        state = DomainState.objects.select_for_update().get(pk=1)
        now = _now()
        previous = _export_locked(now)
        # The pre-import file must itself be usable; failure aborts preparation.
        validate_backup(previous)
        return RestoreSession.objects.create(owner=user, proposed=document, previous=previous,
            source_hash=hashlib.sha256(encode_backup(document)).hexdigest(),
            baseline_epoch=state.epoch, baseline_revision=state.revision,
            expires_at=now + RESTORE_LIFETIME)


def _baseline(session, state):
    if session.baseline_epoch != state.epoch or session.baseline_revision != state.revision:
        _fail("Данные изменились после подготовки копии. Сформируйте свежую копию, скачайте её и подтвердите восстановление ещё раз.")


def download_backup(session_id, user):
    with transaction.atomic():
        state = DomainState.objects.select_for_update().get(pk=1)
        obj = _session(session_id, user, lock=True)
        if obj.completed_at is None:
            _baseline(obj, state)
        # Serialize before recording availability: encoding failure must not
        # grant the next step. The HTTP layer still requires explicit consent.
        encode_backup(obj.previous)
        obj.downloaded_at = timezone.now()
        obj.save(update_fields=["downloaded_at"])
        return deepcopy(obj.previous)


def refresh_restore(session_id, user):
    with transaction.atomic():
        state = DomainState.objects.select_for_update().get(pk=1)
        obj = _session(session_id, user, lock=True)
        if obj.completed_at:
            _fail("Это восстановление уже завершено. Для нового восстановления загрузите файл заново.")
        obj.previous = _export_locked(_now())
        validate_backup(obj.previous)
        obj.baseline_epoch, obj.baseline_revision = state.epoch, state.revision
        obj.downloaded_at = None
        obj.save(update_fields=["previous", "baseline_epoch", "baseline_revision", "downloaded_at"])
        return obj


def _replace(tables):
    # Deferred SQL FKs permit the Ingredient/Price cycle; Python PROTECT must
    # not turn a complete, validated replacement into a sequence of partial edits.
    serial_models = [PriceApplication, DishLine, PreparationLine, AuditEntry]
    with connection.cursor() as cursor:
        old_sequences = {}
        for model in serial_models:
            cursor.execute("SELECT nextval(pg_get_serial_sequence(%s, 'id'))", [model._meta.db_table])
            old_sequences[model] = cursor.fetchone()[0]
        cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        for model in reversed(tuple(TABLES.values())):
            cursor.execute("DELETE FROM " + connection.ops.quote_name(model._meta.db_table))
    for name, model in TABLES.items():
        rows = list(tables[name].values())
        if not rows:
            continue
        dates = [field.attname for field in model._meta.concrete_fields
                 if getattr(field, "auto_now_add", False) or getattr(field, "auto_now", False)]
        saved_dates = {row.pk: {name: getattr(row, name) for name in dates} for row in rows}
        model.objects.bulk_create(rows, batch_size=500)
        if dates:
            for row in rows:
                for field, value in saved_dates[row.pk].items():
                    setattr(row, field, value)
            model.objects.bulk_update(rows, dates, batch_size=500)
    with connection.cursor() as cursor:
        for model in serial_models:
            name = next(name for name, candidate in TABLES.items() if candidate is model)
            top = max([old_sequences[model], *tables[name]])
            # Never rewind below the original sequence: if a later error rolls
            # the data back, future inserts still cannot collide with old IDs.
            cursor.execute("SELECT setval(pg_get_serial_sequence(%s, 'id'), %s, true)", [model._meta.db_table, top])


def apply_restore(session_id, user, *, review_token, confirm_download=False):
    if confirm_download is not True:
        _fail("Скачайте копию текущих данных и явно подтвердите её сохранение перед заменой.")
    with transaction.atomic():
        state = DomainState.objects.select_for_update().get(pk=1)
        obj = _session(session_id, user, lock=True)
        _check_review_token(obj, user, review_token)
        if obj.completed_at:
            return obj
        _baseline(obj, state)
        if obj.downloaded_at is None:
            _fail("Сначала скачайте предимпортную копию текущих данных.")
        document = validate_backup(obj.proposed)
        if hashlib.sha256(encode_backup(document)).hexdigest() != obj.source_hash:
            _fail("Подготовленный файл изменился. Загрузите файл заново.")
        tables = _rows(document)
        now = _now()
        deadlines = []
        for dish in tables["dishes"].values():
            if dish.status == "trashed":
                original_due = dish.purge_at
                dish.purge_at = now + timedelta(microseconds=dish._remaining_microseconds)
                deadlines.append({"dish_id": str(dish.pk), "historical_trashed_at": _json_value(dish.trashed_at),
                    "previous_purge_at": _json_value(original_due), "purge_at": _json_value(dish.purge_at),
                    "remaining_microseconds": dish._remaining_microseconds})
        _replace(tables)
        Operation.objects.all().delete()
        state.epoch = uuid.uuid4()
        state.revision += 1
        state.save(update_fields=["epoch", "revision"])
        AuditEntry.objects.create(group=obj.pk, object_type="DomainState", object_id="1",
            object_name="Восстановление данных", action="restore", source="import", actor_name=current_actor(),
            before={"snapshot_at": obj.previous["snapshot_at"], "counts": data_summary(obj.previous)},
            after={"snapshot_at": document["snapshot_at"], "restored_at": now.isoformat(),
                "counts": data_summary(document), "source_hash": obj.source_hash, "trash_deadlines": deadlines})
        obj.completed_at = now
        obj.save(update_fields=["completed_at"])
        # Force all deferred FK checks before returning success, while the
        # restore and its confirmation receipt can still roll back together.
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            # Do not leak constraint timing into a surrounding transaction
            # (e.g. a subsequent aggregate operation inside the same request).
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        return obj


def cleanup_restore_sessions(*, now=None):
    """Remove expired staging only; this does not change domain data or audit."""
    return RestoreSession.objects.filter(expires_at__lte=_now(now)).delete()[0]
