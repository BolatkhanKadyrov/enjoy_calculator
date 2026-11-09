"""Read-only previews and signed confirmations for purchases and delivery."""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.forms.utils import ErrorList
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from tseh.forms import CommandForm
from tseh.listing import list_context, matches, sorted_rows
from tseh.models import Price, Purchase, UnallocatedDelivery
from tseh.price_forms import PriceDataForm
from tseh.price_views import post_digest
from tseh.purchase_forms import (DeliveryAttachForm, DeliveryForm, DeliveryListForm, DeliveryQuantityFormSet,
    PurchaseForm, PurchaseLineDeleteForm, PurchaseLineFormSet, PurchaseListForm)
from tseh.services import commands, purchases
from tseh.services.purchase_queries import purchase_impact
from tseh.views import analysis_discount, read_snapshot

PREVIEW_SALT = "tseh-purchase-preview-v1"
LINE_FIELDS = ("id", "ingredient", "pack_size", "pack_price", "pack_count", "comment", "is_one_off")


def identity(action, pk=None, line_id=None):
    return {"action": action, "id": str(pk) if pk else None, "line": str(line_id) if line_id else None}


def confirmation(request, action, pk=None, line_id=None):
    try:
        envelope = signing.loads(request.POST.get("preview_token", ""), salt=PREVIEW_SALT, max_age=86400)
        if (envelope["identity"] == identity(action, pk, line_id) and envelope["user"] == str(request.user.pk)
                and envelope["post"] == post_digest(request.POST)
                and envelope["discount"] == str(analysis_discount(request)[0])):
            return envelope["payload"]
    except (signing.BadSignature, KeyError, ValueError, TypeError):
        pass
    return None


def preview_token(request, payload, action, pk=None, line_id=None):
    return signing.dumps({"identity": identity(action, pk, line_id), "user": str(request.user.pk),
        "post": post_digest(request.POST), "discount": str(analysis_discount(request)[0]),
        "payload": json.loads(json.dumps(payload, default=str))}, salt=PREVIEW_SALT, compress=True)


def check_revision(token, state):
    try:
        base = signing.loads(token, salt=commands.SALT)
        if base["epoch"] != str(state.epoch) or base["revision"] != state.revision:
            raise ValidationError("Исходные данные изменились. Откройте форму заново и повторите сравнение.")
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        raise ValidationError("Форма недействительна. Откройте её заново.")


def execute(action, pk, line_id, payload):
    token = payload["token"]
    if action in {"purchase-create", "purchase-edit"}:
        return purchases.save_purchase(token, payload["data"], payload["lines"], purchase_id=pk,
            confirm_zero=payload["confirm_zero"], reason=payload["reason"])
    if action == "line-delete":
        return purchases.delete_purchase_line(token, pk, line_id, **payload["options"])
    if action in {"delivery-create", "delivery-edit"}:
        return purchases.save_delivery(token, payload["data"], delivery_id=pk, reason=payload["reason"])
    if action == "delivery-delete":
        return purchases.delete_delivery(token, pk)
    return purchases.attach_delivery(token, pk, payload["purchase_id"],
        confirm_addition=payload["confirm_addition"], quantities=payload["quantities"])


def confirm_before_lookup(request, action, pk=None, line_id=None):
    """Replay must precede lookup: a successful command may remove its own object."""
    if request.method != "POST" or request.POST.get("intent") != "confirm":
        return None, []
    payload = confirmation(request, action, pk, line_id)
    if payload is None:
        return None, ["Данные изменились или подтверждение недействительно. Проверьте новый предпросмотр и подтвердите ещё раз."]
    if action == "line-delete":
        # Check signed options before replay: the deleted purchase/line may no
        # longer exist, but the current employee permissions still apply.
        options = payload.get("options", {})
        has_new = options.get("replacement_data") is not None
        has_replacement = has_new or options.get("replacement_price_id") is not None
        if has_replacement and not request.access.prices_change:
            raise PermissionDenied
        if has_new and not request.access.prices_add:
            raise PermissionDenied
    try:
        result = execute(action, pk, line_id, payload)
        messages.success(request, {"purchase-create": "Закупка сохранена. Цены доступны как предложения.",
            "purchase-edit": "Закупка исправлена. Доли доставки и связанные цены обновлены.",
            "line-delete": "Строка удалена. Выбранное действие с доставкой выполнено.",
            "delivery-create": "Нераспределённая доставка сохранена.",
            "delivery-edit": "Доставка исправлена. Причина сохранена в журнале.",
            "delivery-delete": "Нераспределённая доставка удалена.",
            "delivery-attach": "Вся сумма доставки перенесена в закупку."}[action])
        if action.startswith("purchase-") or action in {"line-delete", "delivery-attach"}:
            if Purchase.objects.filter(pk=result).exists():
                return redirect("purchase-detail", pk=result), []
            return redirect("purchase-list"), []
        return redirect("delivery-list"), []
    except ValidationError as exc:
        return None, exc.messages


def unavailable(request, errors=()):
    return render(request, "tseh/purchase_unavailable.html", {"active": "purchases", "errors": errors}, status=409)


def add_errors(form, errors):
    for message in errors:
        form.add_error(None, message)


def allocation_rows(plan, before_lines=()):
    before = {line.pk: line for line in before_lines}
    after = {line.pk: line for line in plan.lines}
    keys = list(before) + [key for key in after if key not in before]
    return [{"line": after.get(key) or before[key], "before": before.get(key), "after": after.get(key)} for key in keys]


@login_required
@require_GET
def purchase_list(request):
    with read_snapshot():
        form = PurchaseListForm(request.GET)
        form.is_valid()
        values = dict(form.cleaned_data)
        values["sort"] = values.get("sort") or "newest"
        supplier = values.get("supplier")
        values["supplier"] = str(supplier.pk) if supplier else ""
        rows = []
        for obj in Purchase.objects.select_related("supplier").prefetch_related("lines__ingredient"):
            lines = list(obj.lines.all())
            if supplier and supplier.pk != obj.supplier_id:
                continue
            if not matches(values.get("q"), obj.comment, obj.supplier.name if obj.supplier else "",
                           *(line.ingredient.name for line in lines)):
                continue
            obj.line_count = len(lines)
            obj.item_names = ", ".join(dict.fromkeys(line.ingredient.name for line in lines))
            rows.append(obj)
        key = (lambda obj: obj.delivery_total) if "delivery" in values["sort"] else (lambda obj: (obj.purchase_date, obj.created_at))
        rows = sorted_rows(rows, key, lambda obj: str(obj.pk), values["sort"] in {"newest", "-delivery"})
        context = list_context(request, form, values, rows)
        return render(request, "tseh/purchase_list.html", {**context, "purchases": context["page"].object_list, "active": "purchases"})


@login_required
@require_GET
def purchase_detail(request, pk):
    with read_snapshot():
        obj = get_object_or_404(Purchase.objects.select_related("supplier"), pk=pk)
        lines = list(obj.lines.select_related("ingredient"))
        prices = list(Price.objects.filter(purchase=obj).select_related("ingredient"))
        return render(request, "tseh/purchase_detail.html", {"obj": obj, "lines": lines, "prices": prices, "active": "purchases"})


def line_initial(line):
    return {"id": line.pk, "ingredient": line.ingredient_id,
            **{name: getattr(line, name) for name in LINE_FIELDS if name not in {"id", "ingredient"}}}


def change_unsaved_rows(post, existing_count):
    """Adding/removing an uncommitted row only redisplays posted values, never writes."""
    data = post.copy()
    try:
        total = int(data.get("lines-TOTAL_FORMS", "0"))
        if not 0 <= total <= 100:
            raise ValueError
        if "add_line" in data:
            if total >= 100:
                raise ValidationError("В одной закупке допускается до 100 строк.")
            data["lines-TOTAL_FORMS"] = str(total + 1)
        else:
            index = int(data["remove_line"])
            if not existing_count <= index < total:
                raise ValidationError("Сохранённая строка удаляется отдельным действием в карточке закупки.")
            for target in range(index, total - 1):
                for name in LINE_FIELDS:
                    key, source = f"lines-{target}-{name}", f"lines-{target + 1}-{name}"
                    data.setlist(key, data.getlist(source))
            for name in LINE_FIELDS:
                data.pop(f"lines-{total - 1}-{name}", None)
            data["lines-TOTAL_FORMS"] = str(total - 1)
        data.pop("preview_token", None)
        return data
    except (TypeError, ValueError, KeyError):
        raise ValidationError("Не удалось изменить состав. Обновите форму и повторите действие.")


@login_required
@require_http_methods(["GET", "POST"])
def purchase_editor(request, pk=None):
    action = "purchase-edit" if pk else "purchase-create"
    response, errors = confirm_before_lookup(request, action, pk)
    if response:
        return response
    with read_snapshot() as state:
        obj = Purchase.objects.filter(pk=pk).first() if pk else None
        if pk and obj is None:
            return unavailable(request, errors)
        existing = list(obj.lines.select_related("ingredient")) if obj else []
        data = request.POST if request.method == "POST" else None
        editing_rows = data is not None and ("add_line" in data or "remove_line" in data)
        if editing_rows:
            try:
                data = change_unsaved_rows(data, len(existing))
            except ValidationError as exc:
                errors.extend(exc.messages)
        initial = {"token": commands.stamp(state)}
        if obj:
            initial.update(supplier=obj.supplier_id, purchase_date=obj.purchase_date,
                           delivery_total=obj.delivery_total, comment=obj.comment)
        form = PurchaseForm(data, initial=initial, purchase=obj)
        formset = PurchaseLineFormSet(data, prefix="lines", existing=existing,
            initial=[line_initial(line) for line in existing] or [{}])
        context = {"obj": obj, "action": action, "form": form, "formset": formset,
            "active": "purchases", "discount": analysis_discount(request)[0], "editing_rows": editing_rows}
        if request.method == "GET" or editing_rows:
            if errors:
                form.is_valid()
                add_errors(form, errors)
            elif editing_rows:
                # Row controls preserve incomplete input without presenting unrelated errors.
                form._errors = {}
                for row in formset:
                    row._errors = {}
                formset._errors = [{} for _ in formset]
                formset._non_form_errors = ErrorList()
            return render(request, "tseh/purchase_editor.html", context, status=400 if errors else 200)
        valid_form, valid_lines = form.is_valid(), formset.is_valid()
        if valid_form and valid_lines and not errors:
            try:
                check_revision(form.cleaned_data["token"], state)
                payload = {"token": form.cleaned_data["token"], "data": form.purchase_data(),
                    "lines": [row.line_data() for row in formset], "confirm_zero": form.cleaned_data["confirm_zero"],
                    "reason": form.cleaned_data.get("reason", "")}
                plan = purchases.prepare_purchase(payload["data"], payload["lines"], purchase_id=pk,
                    confirm_zero=payload["confirm_zero"], reason=payload["reason"])
                context.update(preview=True, plan=plan, impact=purchase_impact(plan, context["discount"]),
                    allocation_rows=allocation_rows(plan, existing),
                    preview_token=preview_token(request, payload, action, pk))
            except ValidationError as exc:
                errors.extend(exc.messages)
        add_errors(form, errors)
        return render(request, "tseh/purchase_editor.html", context,
                      status=400 if errors or not (valid_form and valid_lines) else 200)


@login_required
@require_http_methods(["GET", "POST"])
def purchase_line_delete(request, pk, line_id):
    action = "line-delete"
    response, errors = confirm_before_lookup(request, action, pk, line_id)
    if response:
        return response
    with read_snapshot() as state:
        obj = Purchase.objects.filter(pk=pk).first()
        line = obj.lines.select_related("ingredient").filter(pk=line_id).first() if obj else None
        if line is None:
            return unavailable(request, errors)
        last_line = obj.lines.count() == 1
        removed_price = Price.objects.get(purchase=obj, ingredient=line.ingredient)
        needs_replacement = (line.ingredient.current_price_id == removed_price.pk
            and not obj.lines.filter(ingredient=line.ingredient).exclude(pk=line.pk).exists())
        if needs_replacement:
            if not request.access.prices_change:
                raise PermissionDenied
            if request.method == "POST" and request.POST.get("replacement_mode") == "new" and not request.access.prices_add:
                raise PermissionDenied
        data = request.POST if request.method == "POST" else None
        form = PurchaseLineDeleteForm(data, initial={"token": commands.stamp(state)}, last_line=last_line,
            replacement_ingredient=line.ingredient if needs_replacement else None, removed_price=removed_price)
        if needs_replacement and not request.access.prices_add:
            form.fields["replacement_mode"].choices = [choice for choice in form.fields["replacement_mode"].choices
                                                       if choice[0] != "new"]
        replacement_form = PriceDataForm(data, prefix="replacement", ingredient=line.ingredient,
            use_required_attribute=False) if needs_replacement and request.access.prices_add else None
        context = {"obj": obj, "line": line, "form": form, "replacement_form": replacement_form,
            "last_line": last_line, "needs_replacement": needs_replacement, "active": "purchases",
            "discount": analysis_discount(request)[0], "action": action}
        if request.method == "GET":
            return render(request, "tseh/purchase_line_delete.html", context)
        valid = form.is_valid()
        if valid and replacement_form and form.cleaned_data.get("replacement_mode") == "new":
            valid = replacement_form.is_valid()
        if valid and not errors:
            try:
                check_revision(form.cleaned_data["token"], state)
                options = {"delivery_action": form.cleaned_data["delivery_action"],
                           "replacement_price_id": None, "replacement_data": None, "confirm_zero": False}
                if needs_replacement:
                    if form.cleaned_data["replacement_mode"] == "new":
                        options.update(replacement_data=replacement_form.price_data(),
                                       confirm_zero=replacement_form.cleaned_data["confirm_zero"])
                    else:
                        options["replacement_price_id"] = form.cleaned_data["replacement_price"].pk
                plan = purchases.prepare_delete_purchase_line(pk, line_id, **options)
                payload = {"token": form.cleaned_data["token"], "options": options}
                context.update(preview=True, plan=plan, impact=purchase_impact(plan, context["discount"]),
                    allocation_rows=allocation_rows(plan, obj.lines.select_related("ingredient")),
                    preview_token=preview_token(request, payload, action, pk, line_id))
            except ValidationError as exc:
                errors.extend(exc.messages)
        add_errors(form, errors)
        return render(request, "tseh/purchase_line_delete.html", context, status=400 if errors or not valid else 200)


@login_required
@require_GET
def delivery_list(request):
    with read_snapshot():
        form = DeliveryListForm(request.GET)
        form.is_valid()
        values = dict(form.cleaned_data)
        values["sort"] = values.get("sort") or "newest"
        rows = [obj for obj in UnallocatedDelivery.objects.all() if matches(values.get("q"), obj.note)]
        key = (lambda obj: obj.amount) if "amount" in values["sort"] else (lambda obj: obj.created_at)
        rows = sorted_rows(rows, key, lambda obj: str(obj.pk), values["sort"] in {"newest", "-amount"})
        context = list_context(request, form, values, rows)
        return render(request, "tseh/delivery_list.html", {**context, "deliveries": context["page"].object_list,
                                                         "active": "purchases"})


@login_required
@require_http_methods(["GET", "POST"])
def delivery_editor(request, pk=None, action=None):
    action = "delivery-delete" if action == "delete" else ("delivery-edit" if pk else "delivery-create")
    response, errors = confirm_before_lookup(request, action, pk)
    if response:
        return response
    with read_snapshot() as state:
        obj = UnallocatedDelivery.objects.filter(pk=pk).first() if pk else None
        if pk and obj is None:
            return unavailable(request, errors)
        initial = {"token": commands.stamp(state)}
        if obj:
            initial.update(amount=obj.amount, note=obj.note)
        data = request.POST if request.method == "POST" else None
        form = CommandForm(data, initial=initial) if action == "delivery-delete" else DeliveryForm(
            data, initial=initial, editing=obj is not None)
        context = {"obj": obj, "form": form, "action": action, "active": "purchases"}
        if request.method == "GET":
            return render(request, "tseh/delivery_editor.html", context)
        valid = form.is_valid()
        if valid and not errors:
            try:
                check_revision(form.cleaned_data["token"], state)
                payload = {"token": form.cleaned_data["token"]}
                if action != "delivery-delete":
                    payload.update(data=form.delivery_data(), reason=form.cleaned_data.get("reason", ""))
                    context["proposed"] = purchases.prepare_delivery(payload["data"], delivery_id=pk, reason=payload["reason"])
                context.update(preview=True, preview_token=preview_token(request, payload, action, pk))
            except ValidationError as exc:
                errors.extend(exc.messages)
        add_errors(form, errors)
        return render(request, "tseh/delivery_editor.html", context, status=400 if errors or not valid else 200)


@login_required
@require_http_methods(["GET", "POST"])
def delivery_attach(request, pk):
    action = "delivery-attach"
    response, errors = confirm_before_lookup(request, action, pk)
    if response:
        return response
    with read_snapshot() as state:
        obj = UnallocatedDelivery.objects.filter(pk=pk).first()
        if obj is None:
            return unavailable(request, errors)
        data = request.POST if request.method == "POST" else None
        form = DeliveryAttachForm(data, initial={"token": commands.stamp(state)})
        target, quantity_forms, missing = None, None, []
        valid = form.is_valid() if data is not None else False
        selecting = data is not None and request.POST.get("intent") == "select"
        if valid:
            target = form.cleaned_data["purchase"]
            if target.delivery_total + obj.amount > 0:
                missing = list(target.lines.filter(pack_count__isnull=True).select_related("ingredient"))
            if missing:
                quantity_data = None if selecting or "quantities-TOTAL_FORMS" not in data else data
                quantity_forms = DeliveryQuantityFormSet(quantity_data, prefix="quantities",
                    initial=[{"id": line.pk} for line in missing])
                for quantity_form, line in zip(quantity_forms, missing):
                    quantity_form.fields["pack_count"].label = f"{line.ingredient.name}: количество упаковок"
        context = {"obj": obj, "form": form, "target": target,
            "target_lines": list(target.lines.all()) if target else [], "quantity_forms": quantity_forms,
            "active": "purchases", "discount": analysis_discount(request)[0], "action": action}
        if request.method == "GET" or selecting:
            add_errors(form, errors)
            return render(request, "tseh/delivery_attach.html", context, status=400 if errors or (selecting and not valid) else 200)
        if valid and not errors:
            try:
                check_revision(form.cleaned_data["token"], state)
                quantities = {}
                if missing:
                    if not quantity_forms.is_bound:
                        errors.append("Для распределения доставки заполните количество упаковок в строках ниже.")
                        valid = False
                    elif not quantity_forms.is_valid():
                        valid = False
                    else:
                        quantities = {str(row.cleaned_data["id"]): row.cleaned_data["pack_count"] for row in quantity_forms}
                        if len(quantities) != len(missing) or set(quantities) != {str(line.pk) for line in missing}:
                            raise ValidationError("Состав выбранной закупки изменился. Выберите её заново и заполните количества.")
                if valid:
                    payload = {"token": form.cleaned_data["token"], "purchase_id": target.pk,
                               "confirm_addition": form.cleaned_data["confirm_addition"], "quantities": quantities}
                    plan = purchases.prepare_attach_delivery(pk, target.pk,
                        confirm_addition=payload["confirm_addition"], quantities=quantities)
                    context.update(preview=True, plan=plan, impact=purchase_impact(plan, context["discount"]),
                        allocation_rows=allocation_rows(plan, target.lines.select_related("ingredient")),
                        preview_token=preview_token(request, payload, action, pk))
            except ValidationError as exc:
                errors.extend(exc.messages)
        add_errors(form, errors)
        return render(request, "tseh/delivery_attach.html", context, status=400 if errors or not valid else 200)
