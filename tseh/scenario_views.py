"""A complete scenario is previewed, signed and applied as one command."""
import json
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.forms.utils import ErrorList
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.html import format_html
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from tseh.forms import CommandForm
from tseh.models import Price, Purchase
from tseh.price_views import post_digest
from tseh.purchase_views import check_revision
from tseh.scenario_forms import ScenarioFormSet
from tseh.services import commands, scenarios
from tseh.services.scenario_queries import scenario_impact
from tseh.views import analysis_discount, read_snapshot

PREVIEW_SALT = "tseh-scenario-preview-v1"
ROW_FIELDS = ("ingredient", "mode", "saved_price", "new-supplier", "new-price_date", "new-pack_size",
              "new-pack_price", "new-comment", "new-is_one_off", "new-confirm_zero")


def confirmation(request):
    try:
        envelope = signing.loads(request.POST.get("preview_token", ""), salt=PREVIEW_SALT, max_age=86400)
        if (envelope["user"] == str(request.user.pk) and envelope["action"] == "scenario-apply"
                and envelope["path"] == request.path and envelope["post"] == post_digest(request.POST)
                and envelope["discount"] == str(analysis_discount(request)[0])):
            return envelope["payload"]
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        pass
    return None


def signed_preview(request, payload, discount):
    return signing.dumps({"user": str(request.user.pk), "action": "scenario-apply", "path": request.path,
        "post": post_digest(request.POST), "discount": str(discount),
        "payload": json.loads(json.dumps(payload, default=str))}, salt=PREVIEW_SALT, compress=True)


def edit_rows(post):
    data = post.copy()
    try:
        count = int(data.get("rows-TOTAL_FORMS", ""))
        if not 0 <= count <= 100:
            raise ValueError
        if "add_line" in data:
            if count == 100:
                raise ValidationError("В сценарии допускается до 100 изменений цен.")
            data["rows-TOTAL_FORMS"] = str(count + 1)
            data[f"rows-{count}-mode"] = "new"
            data[f"rows-{count}-new-price_date"] = timezone.localdate().isoformat()
        elif "remove_line" in data:
            index = int(data["remove_line"])
            if not 0 <= index < count:
                raise ValueError
            for target in range(index, count - 1):
                for name in ROW_FIELDS:
                    data.setlist(f"rows-{target}-{name}", data.getlist(f"rows-{target + 1}-{name}"))
            for name in ROW_FIELDS:
                data.pop(f"rows-{count - 1}-{name}", None)
            data["rows-TOTAL_FORMS"] = str(count - 1)
        elif "configure_line" in data and not 0 <= int(data["configure_line"]) < count:
            raise ValueError
        data.pop("preview_token", None)
        return data
    except (TypeError, ValueError, KeyError):
        raise ValidationError("Не удалось изменить строки. Откройте сценарий заново и повторите действие.")


def preload_purchase(value):
    try:
        key = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Не удалось открыть исходную закупку. Выберите её из списка закупок.")
    purchase = Purchase.objects.filter(pk=key).first()
    if purchase is None:
        raise ValidationError("Исходная закупка больше недоступна. Откройте актуальный список закупок.")
    prices = Price.objects.filter(purchase=purchase).select_related("ingredient").order_by("ingredient__name", "id")
    rows = [{"ingredient": price.ingredient_id, "mode": "existing", "saved_price": price.pk}
            for price in prices if price.ingredient.current_price_id != price.pk]
    return purchase, rows


@login_required
@require_http_methods(["GET", "POST"])
def scenario_editor(request):
    errors = []
    # Command replay must work even if its selected prices were subsequently deleted.
    if request.method == "POST" and request.POST.get("intent") == "confirm":
        payload = confirmation(request)
        if payload is None:
            errors.append("Данные изменились или подтверждение недействительно. Пересчитайте сценарий и подтвердите ещё раз.")
        else:
            try:
                scenarios.apply_scenario(payload["token"], payload["rows"])
                messages.success(request, format_html('Все цены сценария применены вместе. <a href="{}">Посмотреть журнал</a>.', reverse("journal")))
                return redirect("dashboard")
            except ValidationError as exc:
                errors.extend(exc.messages)
    with read_snapshot() as state:
        data = request.POST if request.method == "POST" else None
        initial_rows, source = [{"mode": "new"}], None
        if request.method == "GET" and request.GET.get("purchase"):
            try:
                source, initial_rows = preload_purchase(request.GET["purchase"])
            except ValidationError as exc:
                errors.extend(exc.messages)
        editing = data is not None and any(key in data for key in ("add_line", "remove_line", "configure_line"))
        if editing:
            try:
                data = edit_rows(data)
            except ValidationError as exc:
                errors.extend(exc.messages)
        initial_command = {"token": commands.stamp(state)}
        form = CommandForm(data if data is not None else (initial_command if errors else None),
                           initial=initial_command)
        formset = ScenarioFormSet(data, initial=initial_rows or [{"mode": "new"}], prefix="rows")
        discount = analysis_discount(request)[0]
        context = {"form": form, "formset": formset, "active": "scenarios", "discount": discount,
                   "source": source, "source_has_changes": bool(initial_rows), "editing_rows": editing,
                   "need_ingredient": not formset.catalog["ingredients"]}
        if request.method == "GET" or editing:
            if errors:
                form.is_valid()
            elif editing:
                form._errors = {}
                for row in formset:
                    row._errors = {}
                    row.new_price_form._errors = {}
                formset._errors = [{} for _ in formset]
                formset._non_form_errors = ErrorList()
            for error in errors:
                form.add_error(None, error)
            return render(request, "tseh/scenario_editor.html", context, status=400 if errors else 200)
        valid_form, valid_rows = form.is_valid(), formset.is_valid()
        if valid_form and valid_rows and not errors:
            try:
                check_revision(form.cleaned_data["token"], state)
                rows = [row.row_data() for row in formset]
                plan = scenarios.prepare_scenario(rows)
                payload = {"token": form.cleaned_data["token"], "rows": rows}
                context.update(preview=True, plan=plan, impact=scenario_impact(plan, discount),
                               preview_token=signed_preview(request, payload, discount))
            except ValidationError as exc:
                errors.extend(exc.messages)
        for error in errors:
            form.add_error(None, error)
        return render(request, "tseh/scenario_editor.html", context,
                      status=400 if errors or not (valid_form and valid_rows) else 200)
