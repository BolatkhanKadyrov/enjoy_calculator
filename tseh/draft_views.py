"""Server-saved catalog drafts, deliberately separate from calculation editors."""
from functools import wraps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from tseh.access import Access
from tseh.draft_forms import (DraftConfirmForm, DraftDeleteForm, DraftForm, DraftLineFormSet,
                             DraftLineForm, DraftListForm, KINDS, SECTIONS)
from tseh.drafts import receipt_context, request_id, saved_redirect
from tseh.listing import list_context, matches
from tseh.models import CatalogDraft, Dish, DishLine, DomainState, Ingredient, Preparation, Supplier
from tseh.services.catalog_drafts import (delete_draft, draft_dependencies, draft_token,
                                        preview_draft, publish_draft, save_draft)
from tseh.views import read_snapshot


MODELS = {"ingredient": Ingredient, "preparation": Preparation, "dish": Dish}


def draft_read_snapshot(view):
    """GET draft, baseline, references and token belong to the same restore epoch."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method == "GET":
            with read_snapshot() as state:
                request.draft_read_state = state
                return view(request, *args, **kwargs)
        return view(request, *args, **kwargs)
    return wrapped


def _access(request):
    return Access(request.user)


def _kinds(access, action="view"):
    return [kind for kind, section in SECTIONS.items() if access.can(f"{section}.{action}")]


def _draft(request, pk, action="view"):
    return get_object_or_404(CatalogDraft.objects.filter(kind__in=_kinds(_access(request), action)), pk=pk)


def _context(request, draft=None):
    access = _access(request)
    return {"active": "drafts", "draft": draft, **receipt_context(request),
            "can_edit": bool(draft and not draft.is_published and access.can(f"{SECTIONS[draft.kind]}.change")),
            "can_publish": bool(draft and not draft.is_published and access.can(f"{SECTIONS[draft.kind]}.add")),
            "can_delete": bool(draft and not draft.is_published and access.can(f"{SECTIONS[draft.kind]}.delete"))}


def _target(draft):
    if not draft.is_published:
        return None
    obj = MODELS[draft.kind].objects.filter(pk=draft.published_object_id).first()
    if obj is None:
        return {"label": "Опубликованная карточка удалена. Здесь сохранён её исходник."}
    return {"label": obj.name, "url": reverse(f"{draft.kind}-detail", args=[obj.pk]),
            "status": obj.get_status_display()}


def _rows(draft, access):
    rows = []
    for line in draft.payload.get("lines", []):
        row = dict(line)
        row["unit_label"] = dict(DishLine.UNITS).get(line.get("unit"), line.get("unit") or "Не указана")
        reference = line.get("component", "")
        kind, _, pk = reference.partition(":")
        obj, url = None, ""
        try:
            if kind == "draft":
                obj = CatalogDraft.objects.filter(pk=pk, kind__in=_kinds(access)).first()
                if obj:
                    url = reverse("draft-detail", args=[obj.pk])
                    row["reference_status"] = "Опубликован" if obj.is_published else "Черновик"
            elif kind in ("ingredient", "preparation") and access.can(f"{SECTIONS[kind]}.view"):
                obj = MODELS[kind].objects.filter(pk=pk).first()
                if obj:
                    url = reverse(f"{kind}-detail", args=[obj.pk])
                    row["reference_status"] = obj.get_status_display()
        except (ValidationError, ValueError):
            pass
        row.update(component_name=obj.name if obj else ("Компонент недоступен" if reference else "Не выбран"), component_url=url)
        rows.append(row)
    return rows


def _details(draft, access):
    p = draft.payload
    if draft.kind == "ingredient":
        supplier = None
        if p.get("supplier") and access.can("suppliers.view"):
            try:
                supplier = Supplier.objects.filter(pk=p["supplier"]).first()
            except (ValidationError, ValueError):
                pass
        return [("Тип позиции", dict(Ingredient._meta.get_field("kind").choices).get(p.get("ingredient_kind"), p.get("ingredient_kind"))),
                ("Базовая единица", dict(Ingredient.UNITS).get(p.get("unit"), p.get("unit"))),
                ("Потери, %", p.get("loss")), ("Количество в упаковке", p.get("pack_size")),
                ("Цена упаковки, ₸", p.get("pack_price")), ("Дата цены", p.get("price_date")),
                ("Поставщик", supplier.name if supplier else ("Недоступен" if p.get("supplier") else "Не указан")),
                ("Нулевая цена подтверждена", "Да" if p.get("zero_price_confirmed") else "Нет")]
    if draft.kind == "preparation":
        return [("Выход после приготовления", p.get("output_quantity")),
                ("Единица выхода", dict(DishLine.UNITS).get(p.get("output_unit"), p.get("output_unit")))]
    return [("Отпускная цена, ₸", p.get("base_price")),
            ("Зелёный порог, %", p.get("green_override") or "Общий порог"),
            ("Оранжевый порог, %", p.get("orange_override") or "Общий порог")]


@login_required
@require_GET
@draft_read_snapshot
def draft_list(request):
    access = _access(request)
    kinds = _kinds(access)
    data = request.GET.copy()
    data.setdefault("state", "draft")
    data.setdefault("sort", "updated")
    form = DraftListForm(data, allowed_kinds=kinds)
    form.is_valid()
    values = dict(form.cleaned_data)
    values["state"] = values.get("state") or "draft"
    values["sort"] = values.get("sort") or "updated"
    query = CatalogDraft.objects.filter(kind__in=kinds)
    counts = {row["kind"]: row["count"] for row in query.filter(published_at__isnull=True).values("kind").annotate(count=Count("pk"))}
    if values.get("kind"):
        query = query.filter(kind=values["kind"])
    if values["state"] != "all":
        query = query.filter(published_at__isnull=values["state"] == "draft")
    rows = [draft for draft in query if matches(values.get("q"), draft.name)]
    sort = values["sort"]
    if sort in ("name", "-name"):
        rows.sort(key=lambda item: (item.name.casefold(), str(item.pk)), reverse=sort == "-name")
    else:
        rows.sort(key=lambda item: (item.updated_at if sort == "updated" else item.created_at, str(item.pk)), reverse=sort != "oldest")
    context = list_context(request, form, values, rows)
    context["list_active"] = bool(values.get("q") or values.get("kind") or values["state"] != "draft")
    context.update(_context(request), rows=context["page"].object_list,
        kind_counts=[{"kind": key, "label": label, "count": counts.get(key, 0)} for key, label in KINDS if key in kinds],
        create_kinds=[{"kind": key, "label": label} for key, label in KINDS if key in _kinds(access, "add")])
    return render(request, "tseh/draft_list.html", context)


@login_required
@require_GET
@draft_read_snapshot
def draft_detail(request, pk):
    draft = _draft(request, pk)
    return render(request, "tseh/draft_detail.html", {**_context(request, draft),
        "details": _details(draft, _access(request)), "lines": _rows(draft, _access(request)), "published_target": _target(draft)})


def _choices(request, draft=None):
    access = _access(request)
    groups = []
    for kind, label in (("ingredient", "Сырьё и упаковка"), ("preparation", "Заготовки")):
        if access.can(f"{SECTIONS[kind]}.view"):
            objects = sorted(MODELS[kind].objects.filter(status="active"), key=lambda item: item.name.casefold())
            groups.append((label, [(f"{kind}:{obj.pk}", obj.name) for obj in objects]))
    query = CatalogDraft.objects.filter(kind__in=[kind for kind in ("ingredient", "preparation") if kind in _kinds(access)])
    if draft:
        query = query.exclude(pk=draft.pk)
    for kind, label in (("ingredient", "Черновики сырья и упаковки"), ("preparation", "Черновики заготовок")):
        objects = sorted((obj for obj in query if obj.kind == kind), key=lambda item: item.name.casefold())
        groups.append((label, [(f"draft:{obj.pk}", obj.name + (" · опубликован" if obj.is_published else "")) for obj in objects]))
    return [(label, entries) for label, entries in groups if entries]


def _editor(request, kind, draft=None):
    state = getattr(request, "draft_read_state", None) or DomainState.objects.get(pk=1)
    if kind not in SECTIONS:
        raise Http404
    if not _access(request).can(f"{SECTIONS[kind]}.{'change' if draft else 'add'}"):
        raise PermissionDenied
    if draft and draft.is_published:
        messages.info(request, "Этот черновик уже опубликован. Изменяйте действующую карточку отдельно.")
        return redirect("draft-detail", pk=draft.pk)
    posted = request.POST.copy() if request.method == "POST" else None
    adding = posted is not None and posted.get("intent") == "add_line" and kind != "ingredient"
    if adding:
        try:
            total = int(posted.get("lines-TOTAL_FORMS", "0"))
            if 0 <= total < 100:
                posted["lines-TOTAL_FORMS"] = str(total + 1)
        except (ValueError, TypeError):
            pass
    suppliers = [(str(obj.pk), obj.name) for obj in Supplier.objects.filter(status="active")] if _access(request).can("suppliers.view") else []
    form = DraftForm(posted, kind=kind, draft=draft, token=draft_token(request.user, kind, draft), supplier_choices=suppliers)
    formset = None
    if kind != "ingredient":
        formset = DraftLineFormSet(posted, prefix="lines", initial=(draft.payload.get("lines") if draft else None) or [{}],
                                  form_kwargs={"component_choices": _choices(request, draft)})
    if posted is not None and not adding:
        valid = form.is_valid()
        valid = (formset.is_valid() if formset is not None else True) and valid
        if valid:
            payload = form.payload()
            if formset is not None:
                payload["lines"] = [{key: row.cleaned_data.get(key, "") for key in ("component", "label", "quantity", "unit", "note")}
                                    for row in formset.forms if row.cleaned_data and not row.cleaned_data.get("DELETE")]
            try:
                saved = save_draft(request.user, kind=kind, name=form.cleaned_data["name"], payload=payload,
                    token=form.cleaned_data["token"], draft_id=draft.pk if draft else None,
                    notes=form.cleaned_data["notes"], source_text=draft.source_text if draft else form.cleaned_data.get("source_text", ""),
                    source="manual")
                messages.success(request, "Черновик сохранён на сервере. Можно вернуться к нему позже.")
                return saved_redirect(request, "draft-detail", pk=saved.pk)
            except ValidationError as exc:
                form.add_error(None, exc.messages)
    return render(request, "tseh/draft_editor.html", {**_context(request, draft), "form": form, "formset": formset,
        "kind": kind, "kind_label": dict(KINDS)[kind], "adding_line": adding,
        "local_revision": f"{state.epoch}:{draft.version if draft else 'new'}",
        "local_request": request_id(request.POST.get("_draft_request")) if request.method == "POST" else ""},
        status=400 if posted is not None and not adding and (form.errors or (formset and formset.errors) or (formset and formset.non_form_errors())) else 200)


@login_required
@require_http_methods(["GET", "POST"])
@draft_read_snapshot
def draft_create(request, kind):
    return _editor(request, kind)


@login_required
@require_http_methods(["GET", "POST"])
@draft_read_snapshot
def draft_edit(request, pk):
    draft = _draft(request, pk, "change")
    return _editor(request, draft.kind, draft)


@login_required
@require_http_methods(["GET", "POST"])
@draft_read_snapshot
def draft_preview(request, pk):
    draft = _draft(request, pk, "add")
    if draft.is_published:
        return redirect("draft-detail", pk=draft.pk)
    errors = []
    form = None
    if request.method == "POST":
        form = DraftConfirmForm(request.POST)
        if form.is_valid():
            try:
                publish_draft(request.user, draft_id=draft.pk, preview_token=form.cleaned_data["preview_token"])
                messages.success(request, "Карточка опубликована и участвует в расчётах. Исходник сохранён в черновиках.")
                return redirect("draft-detail", pk=draft.pk)
            except ValidationError as exc:
                errors = exc.messages
    try:
        preview = preview_draft(request.user, draft_id=draft.pk)
    except ValidationError as exc:
        preview = {"valid": False, "issues": [{"message": message} for message in exc.messages]}
    for issue in preview.get("issues", []):
        field = issue.get("field", "")
        labels = DraftForm(kind=draft.kind, token="").fields
        field = {"kind": "ingredient_kind", "confirm_zero": "zero_price_confirmed"}.get(field, field)
        if field.startswith("lines."):
            parts = field.split(".")
            if len(parts) == 3 and parts[1].isdigit():
                row_field = DraftLineForm.base_fields.get(parts[2])
                issue["location"] = f"Компонент {int(parts[1]) + 1}" + (f" — {row_field.label}" if row_field else "")
                issue["edit_anchor"] = f"draft-line-lines-{parts[1]}"
        elif field in labels:
            issue["location"] = labels[field].label
            issue["edit_anchor"] = "id_draft_output_unit" if field == "output_unit" else f"id_{field}"
        if issue.get("draft_id"):
            dependency = CatalogDraft.objects.filter(pk=issue["draft_id"], kind__in=_kinds(_access(request))).first()
            if dependency:
                issue["url"] = reverse("draft-detail", args=[dependency.pk])
    if not form or not form.errors:
        form = DraftConfirmForm(initial={"preview_token": preview.get("preview_token", "")})
    return render(request, "tseh/draft_preview.html", {**_context(request, draft), "preview": preview, "form": form,
        "publication_errors": errors, "details": _details(draft, _access(request)), "lines": _rows(draft, _access(request))},
        status=400 if request.method == "POST" and (errors or form.errors) else 200)


@login_required
@require_http_methods(["GET", "POST"])
@draft_read_snapshot
def draft_delete(request, pk):
    draft = _draft(request, pk, "delete")
    if draft.is_published:
        raise PermissionDenied("Опубликованный исходник сохраняется для истории.")
    form = DraftDeleteForm(request.POST if request.method == "POST" else None,
                          initial={"token": draft_token(request.user, draft.kind, draft, action="delete")})
    dependencies = draft_dependencies(draft, actor=request.user)
    if request.method == "POST" and form.is_valid():
        try:
            delete_draft(request.user, draft_id=draft.pk, token=form.cleaned_data["token"])
            messages.success(request, "Черновик удалён.")
            return redirect("draft-list")
        except ValidationError as exc:
            form.add_error(None, exc.messages)
    return render(request, "tseh/draft_delete.html", {**_context(request, draft), "form": form, "dependencies": dependencies},
                  status=400 if form.errors else 200)
