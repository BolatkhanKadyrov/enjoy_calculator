"""Two-step price actions. Only an unchanged signed preview can execute a command."""
import hashlib
import json
from copy import copy

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from tseh.listing import list_context, matches, sorted_rows
from tseh.models import Ingredient, Price
from tseh.price_forms import PriceApplyForm, PriceDataForm, PriceDeleteForm, PriceForm, PriceListForm
from tseh.services import commands, prices
from tseh.services.price_queries import history_chart, price_history, price_impact
from tseh.views import analysis_discount, read_snapshot

PREVIEW_SALT = "tseh-price-preview-v1"


def post_digest(post):
    fields = {key: values for key, values in post.lists()
              if key not in {"csrfmiddlewaretoken", "intent", "preview_token"}}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def confirmed_payload(request, pk, price_id, action):
    try:
        envelope = signing.loads(request.POST.get("preview_token", ""), salt=PREVIEW_SALT, max_age=86400)
        if (envelope["user"] == str(request.user.pk) and envelope["ingredient"] == str(pk)
                and envelope["price"] == (str(price_id) if price_id else None)
                and envelope["action"] == action and envelope["post"] == post_digest(request.POST)
                and envelope["discount"] == str(analysis_discount(request)[0])):
            return envelope["payload"]
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        pass
    return None


def execute(action, pk, price_id, payload):
    token = payload["token"]
    if action == "create":
        return prices.create_price(token, pk, payload["data"], apply=payload["apply"], confirm_zero=payload["confirm_zero"])
    if action == "apply":
        return prices.apply_price(token, pk, price_id)
    if action == "correct":
        return prices.correct_price(token, pk, price_id, payload["data"],
                                    confirm_zero=payload["confirm_zero"], reason=payload["reason"])
    return prices.delete_price(token, pk, price_id, replacement_price_id=payload["replacement_price_id"],
                               replacement_data=payload["replacement_data"], confirm_zero=payload["confirm_zero"])


@login_required
@require_GET
def price_list(request, pk):
    with read_snapshot():
        obj = get_object_or_404(Ingredient, pk=pk)
        history = price_history(obj)
        form = PriceListForm(request.GET)
        form.is_valid()
        params = dict(form.cleaned_data)
        params["sort"] = params.get("sort") or "date_desc"
        applied = {row.price.pk for row in history}
        rows = []
        for price in obj.prices.select_related("supplier"):
            if not matches(params.get("q"), price.supplier.name if price.supplier else "", price.comment):
                continue
            if params.get("status") == "current" and price.pk != obj.current_price_id:
                continue
            if params.get("status") == "unapplied" and price.pk in applied:
                continue
            price.unit_value = price.effective_unit_price
            rows.append(price)
        by_price = params["sort"] in {"price", "-price"}
        key = (lambda price: price.unit_value) if by_price else (lambda price: (price.price_date, price.created_at))
        rows = sorted_rows(rows, key, lambda price: str(price.pk), params["sort"] in {"-price", "date_desc"})
        listing = list_context(request, form, params, rows)
        return render(request, "tseh/price_list.html", {**listing, "obj": obj,
            "prices": listing["page"].object_list, "history": history, "chart": history_chart(history), "active": "ingredients"})


@login_required
@require_http_methods(["GET", "POST"])
def price_editor(request, pk, action, price_id=None):
    errors = []
    # Replay a confirmed command BEFORE loading a price that may already have been deleted.
    # The signed body also prevents edits after preview, including flags/replacement choices.
    if request.method == "POST" and request.POST.get("intent") == "confirm":
        payload = confirmed_payload(request, pk, price_id, action)
        if payload is not None:
            if action == "create" and payload.get("apply") and not request.access.prices_change:
                raise PermissionDenied
            if action == "delete":
                if (payload.get("replacement_price_id") or payload.get("replacement_data")) and not request.access.prices_change:
                    raise PermissionDenied
                if payload.get("replacement_data") and not request.access.prices_add:
                    raise PermissionDenied
            try:
                result = execute(action, pk, price_id, payload)
                messages.success(request, {"create": "Цена сохранена.", "apply": "Действующая цена изменена.",
                    "correct": "Цена исправлена. История и расчёты обновлены.", "delete": "Цена удалена. История обновлена."}[action])
                return redirect("price-list", pk=result)
            except ValidationError as exc:
                errors.extend(exc.messages)
        else:
            errors.append("Данные изменились или подтверждение недействительно. Проверьте новый предпросмотр и подтвердите ещё раз.")
    with read_snapshot() as state:
        obj = get_object_or_404(Ingredient.objects.select_related("current_price"), pk=pk)
        price = Price.objects.select_related("supplier").filter(pk=price_id, ingredient=obj).first() if price_id else None
        if price_id and price is None:
            return render(request, "tseh/price_unavailable.html", {"obj": obj, "active": "ingredients"}, status=409)
        if price and price.purchase_id and action in {"correct", "delete"}:
            messages.info(request, "Эта цена рассчитана из закупки. Исправьте закупку или удалите нужную строку в её составе.")
            return redirect("purchase-edit" if action == "correct" else "purchase-detail", pk=price.purchase_id)
        is_current = bool(price and obj.current_price_id == price.pk)
        if action == "delete" and is_current:
            if not request.access.prices_change:
                raise PermissionDenied
            if request.method == "POST" and request.POST.get("replacement_mode") == "new" and not request.access.prices_add:
                raise PermissionDenied
        discount, _ = analysis_discount(request)
        initial = {"token": commands.stamp(state)} if request.method == "GET" else None
        data = request.POST if request.method == "POST" else None
        if action in {"create", "correct"}:
            if initial is not None and price:
                initial.update({name: getattr(price, name) for name in prices.PRICE_FIELDS})
                initial["supplier"] = price.supplier_id
            form = PriceForm(data, initial=initial, ingredient=obj, include_apply=action == "create" and request.access.prices_change, include_reason=action == "correct")
        elif action == "delete":
            form = PriceDeleteForm(data, initial=initial, ingredient=obj, price=price)
            if "replacement_mode" in form.fields and not request.access.prices_add:
                form.fields["replacement_mode"].choices = [choice for choice in form.fields["replacement_mode"].choices if choice[0] != "new"]
        else:
            form = PriceApplyForm(data, initial=initial)
        replacement_form = PriceDataForm(data, prefix="replacement", ingredient=obj,
                                         use_required_attribute=False) if action == "delete" and is_current else None
        context = {"obj": obj, "price": price, "action": action, "form": form, "replacement_form": replacement_form,
                   "is_current": is_current, "active": "ingredients", "discount": discount,
                   "record_unit_price": price.effective_unit_price if price else None,
                   "current_unit_price": obj.current_price.effective_unit_price}
        if request.method == "GET":
            return render(request, "tseh/price_editor.html", context)
        valid = form.is_valid()
        if valid and replacement_form is not None and form.cleaned_data.get("replacement_mode") == "new":
            valid = replacement_form.is_valid()
        if valid:
            try:
                token = form.cleaned_data["token"]
                base = signing.loads(token, salt=commands.SALT)
                if base["epoch"] != str(state.epoch) or base["revision"] != state.revision:
                    raise ValidationError("Исходные данные изменились. Откройте форму заново и повторите сравнение.")
                payload = {"token": token}
                proposed = None
                will_apply = False
                hypothetical = False
                history_before = price_history(obj)
                history_after = history_before
                if action in {"create", "correct"}:
                    values = form.price_data()
                    proposed = copy(price) if price else Price(ingredient=obj)
                    for key, value in values.items():
                        setattr(proposed, key, value)
                    # A copied related-object cache must not retain an old supplier.
                    proposed._state.fields_cache.pop("supplier", None)
                    payload.update(data=values, confirm_zero=form.cleaned_data["confirm_zero"])
                    if action == "create":
                        will_apply = form.cleaned_data.get("apply_now", False)
                        payload["apply"] = will_apply
                        hypothetical = not will_apply
                    else:
                        payload["reason"] = form.cleaned_data["reason"]
                        history_after = price_history(obj, overrides={price.pk: proposed})
                elif action == "apply":
                    if is_current:
                        raise ValidationError("Эта цена уже действует.")
                    proposed, will_apply = price, True
                else:
                    payload.update(replacement_price_id=None, replacement_data=None, confirm_zero=False)
                    if is_current:
                        will_apply = True
                        if form.cleaned_data["replacement_mode"] == "new":
                            values = replacement_form.price_data()
                            payload.update(replacement_data=values, confirm_zero=replacement_form.cleaned_data["confirm_zero"])
                            proposed = Price(ingredient=obj, **values)
                        else:
                            proposed = form.cleaned_data["replacement_price"]
                            payload["replacement_price_id"] = proposed.pk
                    history_after = price_history(obj, excluded={price.pk})
                changes_current = action in {"create", "apply"} or is_current
                impact = price_impact(obj, proposed, discount) if proposed and changes_current else {"preparations": [], "dishes": []}
                outcome = {"create": "Новая цена станет действующей." if will_apply else "Сохранится только предложение. Действующая цена не изменится.",
                    "apply": "Выбранная цена станет действующей.",
                    "correct": "Будут исправлены все применения этой записи на прежних датах. Новая точка применения не добавится.",
                    "delete": "Цена и её прежние применения будут удалены; исходные значения сохранятся в журнале."}[action]
                payload = json.loads(json.dumps(payload, default=str))
                envelope = {"user": str(request.user.pk), "ingredient": str(pk), "price": str(price_id) if price_id else None,
                            "action": action, "post": post_digest(request.POST), "payload": payload,
                            "discount": str(discount)}
                context.update(preview=True, preview_token=signing.dumps(envelope, salt=PREVIEW_SALT, compress=True),
                    proposed=proposed, proposed_unit_price=proposed.effective_unit_price if proposed else None,
                    impact=impact, impact_hypothetical=hypothetical, history_before=history_before, history_after=history_after,
                    will_apply=will_apply, outcome=outcome)
            except ValidationError as exc:
                errors.extend(exc.messages)
            except (signing.BadSignature, KeyError, TypeError, ValueError):
                errors.append("Форма недействительна. Откройте её заново.")
        for error in errors:
            form.add_error(None, error)
        return render(request, "tseh/price_editor.html", context, status=400 if errors or not valid else 200)
