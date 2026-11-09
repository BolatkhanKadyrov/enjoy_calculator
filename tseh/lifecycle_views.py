"""Explicit lifecycle transitions; no deletion occurs on a GET request."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from tseh.forms import CommandForm
from tseh.listing import list_context, matches, sorted_rows
from tseh.models import Dish, Ingredient, Preparation, Supplier
from tseh.services import commands, lifecycle
from tseh.views import read_snapshot
from tseh.access import visible_dependencies

TRANSITIONS = {
    "archive": ("Убрать в архив", "Блюдо исчезнет из активного ассортимента. Состав сохранится; блюдо можно вернуть в работу.", "active"),
    "trash": ("Переместить в корзину", "Блюдо будет окончательно удалено после 30 дней при очередной очистке. До удаления его можно восстановить в архив.", "archived"),
    "restore": ("Восстановить в архив", "Блюдо вернётся в архив. Возврат в активный ассортимент — отдельное действие.", "trashed"),
    "activate": ("Вернуть в ассортимент", "Блюдо снова появится в активном списке. Себестоимость рассчитывается по действующим ценам.", "archived"),
}
CATALOG = {"supplier": (Supplier, "suppliers"), "ingredient": (Ingredient, "ingredients"),
           "preparation": (Preparation, "preparations")}


class ArchiveListForm(forms.Form):
    q = forms.CharField(label="Название", max_length=160, required=False,
                        widget=forms.TextInput(attrs={"type": "search", "placeholder": "Найти запись"}))
    status = forms.ChoiceField(label="Раздел", choices=[("archived", "Архив"), ("trashed", "Корзина")], required=False)
    kind = forms.ChoiceField(label="Тип записи", choices=[("", "Все"), ("dish", "Блюда"), ("ingredient", "Сырьё и упаковка"),
        ("preparation", "Заготовки"), ("supplier", "Поставщики")], required=False)
    sort = forms.ChoiceField(label="Сортировка", choices=[("name", "Название: А–Я"), ("-name", "Название: Я–А"),
        ("newest", "Сначала недавно перемещённые"), ("oldest", "Сначала давно перемещённые")], required=False)


@login_required
@require_GET
def archive(request):
    form = ArchiveListForm(request.GET)
    permission_by_kind = {"dish": "dishes.view", "ingredient": "ingredients.view",
                          "preparation": "preparations.view", "supplier": "suppliers.view"}
    form.fields["kind"].choices = [(value, label) for value, label in form.fields["kind"].choices
                                   if not value or request.access.can(permission_by_kind[value])]
    form.is_valid()
    params = {**form.cleaned_data, "status": form.cleaned_data.get("status") or "archived",
              "sort": form.cleaned_data.get("sort") or "name"}
    with read_snapshot():
        rows = []
        for kind, model, label in [("dish", Dish, "Блюдо"), ("ingredient", Ingredient, "Сырьё / упаковка"),
                                   ("preparation", Preparation, "Заготовка"), ("supplier", Supplier, "Поставщик")]:
            if not request.access.can(permission_by_kind[kind]):
                continue
            if (params["status"] == "archived" and kind != "dish") or (params.get("kind") and params["kind"] != kind):
                continue
            for obj in model.objects.filter(status=params["status"]):
                if matches(params.get("q"), obj.name):
                    obj.trash_kind, obj.trash_type = kind, label
                    rows.append(obj)
        sort = params["sort"]
        key = (lambda obj: obj.name.casefold()) if "name" in sort else (
            lambda obj: obj.trashed_at if obj.status == "trashed" else obj.archived_at)
        rows = sorted_rows(rows, key, lambda obj: str(obj.pk), sort in {"-name", "newest"})
        context = list_context(request, form, params, rows)
        return render(request, "tseh/archive.html", {**context, "active": "archive", "state": params["status"],
            "archived_count": Dish.objects.filter(status="archived").count() if request.access.dishes_view else 0,
            "trashed_count": sum(model.objects.filter(status="trashed").count() for model, permission in
                ((Dish, "dishes.view"), (Ingredient, "ingredients.view"), (Preparation, "preparations.view"), (Supplier, "suppliers.view"))
                if request.access.can(permission))})


@login_required
@require_http_methods(["GET", "POST"])
def dish_transition(request, pk, action):
    if action not in TRANSITIONS:
        raise Http404
    title, explanation, expected = TRANSITIONS[action]
    form = CommandForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            lifecycle.transition_dish(form.cleaned_data["token"], pk, action)
            messages.success(request, {"archive": "Блюдо перемещено в архив.", "trash": "Блюдо перемещено в корзину на 30 дней.",
                "restore": "Блюдо восстановлено в архив.", "activate": "Блюдо возвращено в ассортимент."}[action])
            return redirect("dish-detail", pk=pk)
        except ValidationError as exc:
            for message in visible_dependencies(request.access, exc.messages):
                form.add_error(None, message)
    with read_snapshot() as state:
        obj = get_object_or_404(Dish, pk=pk)
        if request.method == "GET":
            form = CommandForm(initial={"token": commands.stamp(state)})
        return render(request, "tseh/lifecycle_confirm.html", {"form": form, "obj": obj, "title": title,
            "explanation": explanation, "can_apply": obj.status == expected, "active": "archive" if obj.status != "active" else "dishes",
            "danger": action == "trash", "cancel_url": "dish-detail"}, status=400 if form.errors else 200)


@login_required
@require_http_methods(["GET", "POST"])
def catalog_transition(request, kind, pk, action):
    if kind not in CATALOG:
        raise Http404
    if action not in {"trash", "restore", "purge"}:
        raise Http404
    model, back = CATALOG[kind]
    form = CommandForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            lifecycle.transition_catalog(form.cleaned_data["token"], kind, pk, action)
            messages.success(request, {"trash": "Запись перемещена в корзину без срока удаления.",
                "restore": "Запись восстановлена в справочник.", "purge": "Запись удалена окончательно. Журнал сохранён."}[action])
            return redirect(back if action == "restore" else "/archive/?status=trashed")
        except ValidationError as exc:
            for message in visible_dependencies(request.access, exc.messages):
                form.add_error(None, message)
    with read_snapshot() as state:
        obj = get_object_or_404(model, pk=pk)
        if request.method == "GET":
            form = CommandForm(initial={"token": commands.stamp(state)})
        dependencies = lifecycle.catalog_dependencies(kind, pk)
        return render(request, "tseh/catalog_delete.html", {"form": form, "obj": obj, "dependencies": visible_dependencies(request.access, dependencies),
            "back": back, "active": "archive" if obj.status == "trashed" else back, "action": action, "kind": kind,
            "title": {"trash": "Переместить в корзину", "restore": "Восстановить в справочник", "purge": "Удалить окончательно"}[action],
            "can_apply": (obj.status == "active" if action == "trash" else obj.status == "trashed") and (action == "restore" or not dependencies)}, status=400 if form.errors else 200)
