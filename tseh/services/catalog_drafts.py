"""Persist incomplete catalogue input without placing it in the calculation graph."""
from contextlib import contextmanager
import hashlib
import json
import uuid

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from tseh.access import Access
from tseh.audit_actor import audit_actor
from tseh.catalog_draft_payload import KINDS, SECTIONS, component_reference, text_value, validate_draft_payload
from tseh.calculation import unit_price
from tseh.calculation.recipes import Prep
from tseh.forms import DishForm, IngredientCreateForm, LineForm, PreparationForm
from tseh.models import AccountAccess, CatalogDraft, Dish, DomainState, Ingredient, Preparation
from . import commands
from .queries import catalog, proposed_components

SALT = "tseh-catalog-draft-v1"
PREVIEW_SALT = "tseh-catalog-draft-publication-v1"
TOKEN_LIFETIME = 3600
DRAFT_FIELDS = ["kind", "name", "payload", "source_text", "notes", "source", "version",
                "published_object_id", "published_at", "created_at", "updated_at"]


def _identity(actor):
    if not getattr(actor, "is_authenticated", False):
        raise PermissionDenied
    user = get_user_model().objects.filter(pk=actor.pk, is_active=True).first()
    profile = AccountAccess.objects.filter(user_id=actor.pk).first()
    if user is None or profile is None:
        raise PermissionDenied
    user.account_access = profile
    return user, Access(user, profile)


def _allow(access, kind, action):
    if not isinstance(kind, str) or kind not in SECTIONS or not access.can(f"{SECTIONS[kind]}.{action}"):
        raise PermissionDenied("Недостаточно прав для этого черновика.")


@contextmanager
def _write(actor):
    with transaction.atomic():
        list(AccountAccess.objects.select_for_update().filter(is_owner=True).order_by("pk"))
        AccountAccess.objects.select_for_update().filter(user_id=getattr(actor, "pk", None)).first()
        current, access = _identity(actor)
        state = DomainState.objects.select_for_update().get(pk=1)
        with audit_actor(current.username):
            yield current, access, state


def draft_token(actor, kind, draft=None, *, new_id=None, action="save"):
    current, access = _identity(actor)
    if draft is not None:
        kind = draft.kind
    _allow(access, kind, "delete" if action == "delete" else "change" if draft else "add")
    if action not in {"save", "delete"} or (action == "delete" and draft is None):
        raise ValidationError("Некорректное действие черновика.")
    state = DomainState.objects.get(pk=1)
    return signing.dumps({"actor": current.pk, "actor_version": current.account_access.version,
        "kind": kind, "target": str(draft.pk if draft else uuid.UUID(str(new_id)) if new_id else uuid.uuid4()),
        "version": draft.version if draft else None, "digest": _digest(draft) if draft else None,
        "action": action, "command": commands.stamp(state)}, salt=SALT)


def _token(value, actor, *, salt=SALT):
    try:
        data = signing.loads(value, salt=salt, max_age=TOKEN_LIFETIME)
        if (not isinstance(data, dict) or data.get("actor") != actor.pk
                or data.get("actor_version") != actor.account_access.version):
            raise ValueError
        return data
    except (signing.BadSignature, TypeError, ValueError) as exc:
        raise ValidationError("Подтверждение черновика устарело или недействительно. Откройте карточку заново.") from exc


def _snapshot(draft):
    return commands.snapshot(draft, DRAFT_FIELDS)


def _digest(draft):
    data = {key: getattr(draft, key) for key in ("kind", "name", "payload", "source_text", "notes", "source")}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def validate_draft_links(drafts):
    """Check known draft kinds and cycles, allowing unresolved references to survive."""
    drafts = {row.pk: row for row in drafts}
    edges = {}
    for row in drafts.values():
        if row.is_published:
            continue
        edges[row.pk] = []
        for line in row.payload.get("lines", []):
            prefix, target_id = component_reference(line["component"])
            target = drafts.get(target_id) if prefix == "draft" else None
            if target is not None:
                if target.kind not in {"ingredient", "preparation"}:
                    raise ValidationError("Блюдо нельзя использовать как компонент черновика.")
                if not target.is_published:
                    edges[row.pk].append(target.pk)
    visiting, visited = set(), set()
    # Iterative traversal also handles a long imported chain without recursion limits.
    for start in edges:
        stack = [(start, False)]
        while stack:
            node, leaving = stack.pop()
            if leaving:
                visiting.discard(node)
                visited.add(node)
            elif node in visiting:
                raise ValidationError("В составе черновиков образуется цикл. Уберите повторную зависимость.")
            elif node not in visited:
                visiting.add(node)
                stack.append((node, True))
                stack.extend((target, False) for target in reversed(edges.get(node, [])))


def save_draft(actor, *, token, kind, name, payload, draft_id=None, notes="", source_text="", source="manual"):
    with _write(actor) as (current, access, _state):
        supplied = _token(token, current)
        if supplied.get("action") != "save" or supplied.get("kind") != kind:
            raise ValidationError("Подтверждение относится к другому действию или типу карточки.")
        target_id = uuid.UUID(supplied["target"])
        if draft_id is not None and str(target_id) != str(draft_id):
            raise ValidationError("Подтверждение относится к другой карточке.")
        existing = CatalogDraft.objects.select_for_update().filter(pk=target_id).first()
        actual_kind = existing.kind if existing else kind
        if kind != actual_kind:
            raise ValidationError("Тип сохранённого черновика менять нельзя.")
        _allow(access, actual_kind, "add" if supplied["version"] is None else "change")
        if not isinstance(source, str) or source not in {"manual", "import"} or (source == "import" and not access.is_owner):
            raise PermissionDenied("Импорт черновиков доступен только владельцу.")
        name = text_value(name, 160, "Название", required=True).strip()
        notes = text_value(notes, 8000, "Заметки")
        source_text = text_value(source_text, 20000, "Исходный текст")
        payload = validate_draft_payload(actual_kind, payload)
        change = {"id": str(target_id), "kind": actual_kind, "name": name, "payload": payload,
                  "notes": notes, "source_text": source_text, "source": source, "actor": current.pk}

        def apply(group):
            draft = CatalogDraft.objects.select_for_update().filter(pk=target_id).first()
            if ((draft is None) != (supplied["version"] is None)
                    or (draft is not None and (draft.version != supplied["version"]
                                               or supplied.get("digest") != _digest(draft)))):
                raise commands.StaleWrite("Черновик изменился. Откройте свежую карточку.")
            if draft is not None and draft.is_published:
                raise ValidationError("Черновик уже опубликован. Изменяйте карточку в рабочем справочнике.")
            if draft is not None and source_text and source_text != draft.source_text:
                raise ValidationError("Исходный текст сохраняется неизменным. Используйте заметки для уточнений.")
            before = _snapshot(draft) if draft else None
            if draft is None:
                draft = CatalogDraft(id=target_id, kind=actual_kind, source=source, source_text=source_text)
            else:
                draft.version += 1
            draft.name, draft.payload, draft.notes = name, payload, notes
            others = list(CatalogDraft.objects.exclude(pk=target_id))
            validate_draft_links([*others, draft])
            draft.full_clean()
            draft.save()
            commands.audit(group, draft, "draft_update" if before else "draft_create", before, _snapshot(draft))
            return draft

        with commands.audit_source(source):
            saved_id = commands.command(supplied["command"], "catalog-draft-save", change, apply)
        return CatalogDraft.objects.get(pk=saved_id)


def draft_dependencies(draft, actor=None):
    access = _identity(actor)[1] if actor is not None else None
    dependencies = []
    label = " ".join(draft.name.casefold().split())
    for row in CatalogDraft.objects.filter(published_at__isnull=True).exclude(pk=draft.pk):
        found = False
        for line in row.payload.get("lines", []):
            prefix, target = component_reference(line["component"])
            if ((prefix == "draft" and target == draft.pk)
                    or (not line["component"] and " ".join(line["label"].casefold().split()) == label)):
                found = True
                break
        if found:
            visible = access is None or access.can(f"{SECTIONS[row.kind]}.view")
            dependencies.append({"id": row.pk if visible else None,
                "name": row.name if visible else "Черновик из недоступного раздела", "kind": row.kind if visible else None})
    return dependencies


def delete_draft(actor, draft_id, token):
    with _write(actor) as (current, access, _state):
        supplied = _token(token, current)
        if supplied.get("action") != "delete" or supplied.get("target") != str(draft_id):
            raise ValidationError("Подтверждение относится к другой карточке или действию.")
        draft = CatalogDraft.objects.select_for_update().filter(pk=draft_id).first()
        kind = draft.kind if draft else supplied["kind"]
        _allow(access, kind, "delete")

        def apply(group):
            obj = CatalogDraft.objects.select_for_update().filter(pk=draft_id).first()
            if obj is None or obj.version != supplied["version"] or supplied.get("digest") != _digest(obj):
                raise commands.StaleWrite("Черновик изменился или уже удалён. Откройте список заново.")
            if obj.is_published:
                raise ValidationError("Опубликованный исходник и квитанция сохраняются. Удаление недоступно.")
            dependencies = draft_dependencies(obj, current)
            if dependencies:
                raise ValidationError("Черновик используется: " + "; ".join(item["name"] for item in dependencies))
            commands.audit(group, obj, "draft_delete", _snapshot(obj), None)
            target_id = obj.pk
            obj.delete()
            obj.pk = target_id
            return obj

        return commands.command(supplied["command"], "catalog-draft-delete",
                                {"id": str(draft_id), "actor": current.pk}, apply)


def _form_issues(form, prefix=""):
    return [{"field": prefix + key, "message": str(message)}
            for key, messages in form.errors.items() for message in messages]


def _resolved_component(line, access, index):
    prefix, target = component_reference(line["component"])
    issue = {"field": f"lines.{index}.component"}
    if prefix == "draft":
        dependency = CatalogDraft.objects.filter(pk=target).first()
        if dependency is None:
            return None, {**issue, "message": "Связанный черновик удалён или не найден. Выберите компонент."}
        visible = access.can(f"{SECTIONS[dependency.kind]}.view")
        if not visible:
            return None, {**issue, "message": "Нет доступа к связанному черновику."}
        issue["draft_id"] = dependency.pk
        if dependency.kind not in {"ingredient", "preparation"}:
            return None, {**issue, "message": "Блюдо нельзя использовать как компонент."}
        if not dependency.is_published:
            return None, {**issue, "message": f"Сначала опубликуйте черновик «{dependency.name}»."}
        prefix, target = dependency.kind, dependency.published_object_id
    if prefix not in {"ingredient", "preparation"}:
        return None, {**issue, "message": "Выберите компонент для этой строки."}
    if not access.can(f"{SECTIONS[prefix]}.view"):
        return None, {**issue, "message": "Нет доступа к этому справочнику компонентов."}
    model = Ingredient if prefix == "ingredient" else Preparation
    component = model.objects.filter(pk=target).first()
    if component is None:
        return None, {**issue, "message": "Опубликованная карточка компонента удалена или не найдена."}
    if component.status != "active":
        return None, {**issue, "message": "Компонент находится в корзине. Сначала восстановите его."}
    return f"{prefix}:{target}", None


def _prepare(draft, access):
    response = {"draft": draft, "valid": False, "issues": [], "preview_token": None,
                "summary": [("Название", draft.name), ("Тип", draft.get_kind_display())], "result": None}
    if draft.is_published:
        response["issues"].append({"message": "Черновик уже опубликован; исходник и квитанция сохранены."})
        return response, None, []
    try:
        payload = validate_draft_payload(draft.kind, draft.payload)
        validate_draft_links(list(CatalogDraft.objects.all()))
    except ValidationError as exc:
        response["issues"].extend({"message": text} for text in exc.messages)
        return response, None, []
    shared = {"name": draft.name, "token": "draft-publication-validation"}
    if draft.kind == "ingredient":
        if not payload["ingredient_kind"]:
            response["issues"].append({"field": "ingredient_kind", "message": "Выберите сырьё или упаковку."})
        form = IngredientCreateForm({**shared, **payload, "kind": payload["ingredient_kind"],
                                     "confirm_zero": payload["zero_price_confirmed"]})
    elif draft.kind == "preparation":
        form = PreparationForm({**shared, **payload})
    else:
        thresholds = {f"{key}_{suffix}": value for key in ("green", "orange")
            for suffix, value in (("mode", "custom" if payload[f"{key}_override"] else "default"),
                                  ("value", payload[f"{key}_override"]))}
        form = DishForm({**shared, **payload, **thresholds})
    if not form.is_valid():
        response["issues"].extend(_form_issues(form))
    lines = []
    if draft.kind != "ingredient":
        for index, line in enumerate(payload["lines"]):
            reference, issue = _resolved_component(line, access, index)
            if issue:
                response["issues"].append(issue)
                continue
            row_form = LineForm({"component": reference, "quantity": line["quantity"], "unit": line["unit"]},
                                preparation_recipe=draft.kind == "preparation")
            if not row_form.is_valid():
                response["issues"].extend(_form_issues(row_form, f"lines.{index}."))
                continue
            row = row_form.cleaned_data
            lines.append({"ingredient_id": row["ingredient"].pk if row["ingredient"] else None,
                          "preparation_id": row["preparation"].pk if row["preparation"] else None,
                          "quantity": row["quantity"], "unit": row["unit"]})
    if response["issues"]:
        return response, None, lines
    clean = form.cleaned_data
    try:
        if draft.kind == "ingredient":
            data = {key: clean[key] for key in ("name", "kind", "loss", "unit", "pack_size", "pack_price", "price_date")}
            data["supplier_id"] = clean["supplier"].pk if clean["supplier"] else None
            if data["supplier_id"] is not None and not access.can("suppliers.view"):
                raise ValidationError("Нет доступа к выбранному поставщику.")
            result = {"unit_price": unit_price(data["pack_price"], data["pack_size"])}
            response["summary"].extend([("Цена упаковки, ₸", str(data["pack_price"])),
                ("Фасовка", str(data["pack_size"]) + " " + dict(Ingredient.UNITS)[data["unit"]]),
                ("Первичные потери, %", str(data["loss"]))])
        else:
            commands.validate_lines(lines, preparation_recipe=draft.kind == "preparation")
            if draft.kind == "dish":
                data = {key: clean[key] for key in ("name", "base_price", "green_override", "orange_override")}
                commands.validate_dish_thresholds(Dish(), data, is_new=True)
                result = catalog().calculate(proposed_components(lines))
                response["summary"].extend([("Себестоимость порции, ₸", str(result.total)),
                                            ("Отпускная цена, ₸", str(data["base_price"]))])
            else:
                data = {key: clean[key] for key in ("name", "output_quantity", "output_unit")}
                preview_key = ("draft", draft.pk)
                graph = catalog({preview_key: Prep(data["output_quantity"], data["output_unit"], proposed_components(lines))})
                result = graph.prep_costs[preview_key]
                response["summary"].extend([("Стоимость замеса, ₸", str(result.total)),
                    ("Выход", str(data["output_quantity"]) + " " + dict(PreparationForm().fields["output_unit"].choices)[data["output_unit"]])])
        response.update(valid=True, result=result)
        return response, data, lines
    except (ValidationError, ValueError) as exc:
        response["issues"].extend({"message": message} for message in (exc.messages if isinstance(exc, ValidationError) else [str(exc)]))
        return response, None, lines


def preview_draft(actor, draft_id):
    current, access = _identity(actor)
    with transaction.atomic():
        state = DomainState.objects.select_for_update().get(pk=1)
        draft = CatalogDraft.objects.get(pk=draft_id)
        _allow(access, draft.kind, "add")
        response, _data, _lines = _prepare(draft, access)
        if response["valid"]:
            response["preview_token"] = signing.dumps({"actor": current.pk,
                "actor_version": current.account_access.version, "draft": str(draft.pk), "version": draft.version,
                "epoch": str(state.epoch), "revision": state.revision, "digest": _digest(draft),
                "command": commands.stamp(state)}, salt=PREVIEW_SALT)
        return response


def publish_draft(actor, draft_id, preview_token):
    with _write(actor) as (current, access, state):
        supplied = _token(preview_token, current, salt=PREVIEW_SALT)
        draft = CatalogDraft.objects.select_for_update().get(pk=draft_id)
        _allow(access, draft.kind, "add")
        if (supplied.get("draft") != str(draft.pk) or supplied.get("epoch") != str(state.epoch)
                or supplied.get("digest") != _digest(draft)):
            raise commands.StaleWrite("Черновик или данные изменились. Выполните новый предварительный расчёт.")
        if draft.is_published and supplied.get("version") == draft.version - 1:
            return draft.published_object_id
        if supplied.get("version") != draft.version or supplied.get("revision") != state.revision:
            raise commands.StaleWrite("Черновик или данные изменились. Выполните новый предварительный расчёт.")
        preview, data, lines = _prepare(draft, access)
        if not preview["valid"]:
            raise ValidationError([issue["message"] for issue in preview["issues"]])
        before = _snapshot(draft)
        with commands.audit_source("manual"):
            if draft.kind == "ingredient":
                result = commands.create_ingredient(supplied["command"], data,
                                                     confirm_zero=draft.payload["zero_price_confirmed"])
            elif draft.kind == "preparation":
                result = commands.save_preparation(supplied["command"], data, lines)
            else:
                result = commands.save_dish(supplied["command"], data, lines)
            draft.published_object_id, draft.published_at = result, timezone.now()
            draft.version += 1
            draft.save(update_fields=["published_object_id", "published_at", "version", "updated_at"])
            group = uuid.UUID(signing.loads(supplied["command"], salt=commands.SALT)["operation"])
            commands.audit(group, draft, "draft_publish", before, _snapshot(draft))
        return result
