"""Bounded, lossless text inputs. Strict numerical validation belongs to publication."""
from copy import deepcopy
import uuid

from django.core.exceptions import ValidationError

KINDS = {"ingredient", "preparation", "dish"}
SECTIONS = {"ingredient": "ingredients", "preparation": "preparations", "dish": "dishes"}
DEFAULTS = {
    "ingredient": {"unit": "", "ingredient_kind": "", "loss": "", "supplier": "",
                   "price_date": "", "pack_size": "", "pack_price": "", "zero_price_confirmed": False},
    "preparation": {"output_quantity": "", "output_unit": "", "lines": []},
    "dish": {"base_price": "", "green_override": "", "orange_override": "", "lines": []},
}
LINE_DEFAULTS = {"component": "", "label": "", "quantity": "", "unit": "", "note": ""}


def text_value(value, limit, label, *, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValidationError(f"{label}: требуется текст длиной до {limit} символов.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{label}: некорректный символ Unicode.") from exc
    if "\x00" in value:
        raise ValidationError(f"{label}: недопустимый нулевой символ.")
    return value


def component_reference(value):
    if not value:
        return None, None
    try:
        prefix, identifier = value.split(":", 1)
        parsed = uuid.UUID(identifier)
        if prefix not in {"draft", "ingredient", "preparation"}:
            raise ValueError
    except (AttributeError, ValueError, TypeError) as exc:
        raise ValidationError("Ссылка компонента недействительна.") from exc
    return prefix, parsed


def validate_draft_payload(kind, payload):
    if not isinstance(kind, str) or kind not in KINDS or not isinstance(payload, dict):
        raise ValidationError("Неизвестный тип или структура черновика.")
    if set(payload) - set(DEFAULTS[kind]):
        raise ValidationError("В черновике есть неизвестные поля.")
    result = deepcopy(DEFAULTS[kind])
    result.update(payload)
    for key, value in result.items():
        if key == "lines":
            if not isinstance(value, list) or len(value) > 100:
                raise ValidationError("В черновике допускается до 100 строк состава.")
            rows = []
            for raw in value:
                if not isinstance(raw, dict) or set(raw) - set(LINE_DEFAULTS):
                    raise ValidationError("Некорректная структура строки состава.")
                row = {**LINE_DEFAULTS, **raw}
                for field, limit in (("component", 64), ("label", 500), ("quantity", 120), ("unit", 32), ("note", 1024)):
                    text_value(row[field], limit, f"Строка: {field}")
                component_reference(row["component"])
                rows.append(row)
            result[key] = rows
        elif key == "zero_price_confirmed":
            if type(value) is not bool:
                raise ValidationError("Подтверждение нулевой цены должно быть логическим значением.")
        else:
            text_value(value, 32 if key in {"unit", "output_unit", "ingredient_kind"} else 120, key)
    if kind == "ingredient":
        if result["ingredient_kind"] not in {"", "ingredient", "packaging"}:
            raise ValidationError("Выберите сырьё или упаковку.")
        if result["supplier"]:
            try:
                uuid.UUID(result["supplier"])
            except ValueError as exc:
                raise ValidationError("Некорректная ссылка поставщика.") from exc
    return result
