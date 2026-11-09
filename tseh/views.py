import hashlib
import json
import uuid
from copy import copy
from decimal import Decimal
from contextlib import contextmanager
from fractions import Fraction

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import connection, transaction, DatabaseError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods
from django.utils import timezone

from tseh.calculation.recipes import Prep
from tseh.calculation.pricing import assess, effective_thresholds, threshold_state
from tseh.drafts import draft_context, receipt_context, saved_redirect
from tseh.forms import DishForm, DiscountForm, IngredientCreateForm, IngredientEditForm, LineFormSet, PreparationForm, SupplierForm, ThresholdForm
from tseh.models import AuditEntry, Dish, DomainState, Ingredient, Preparation, Supplier
from tseh.services import commands
from tseh.services.queries import catalog, components, dish_calculation, general_settings, proposed_components
from tseh.services.uniqueness import uniqueness_report
from tseh.listing import KINDS, list_context, matches, parameters, sorted_rows


@contextmanager
def read_snapshot():
    # Short read lock also covers token generation and rendering. No lock survives the response.
    with transaction.atomic():
        yield DomainState.objects.select_for_update().get(pk=1)


def add_error(form, exc):
    for message in exc.messages:
        form.add_error(None, message)


def preview_digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def confirmed(request, payload):
    try:
        return signing.loads(request.POST.get("preview_token", ""), salt="preview", max_age=3600) == preview_identity(request, payload)
    except signing.BadSignature:
        return False


def preview_identity(request, payload):
    return {"digest": preview_digest(payload), "user": str(request.user.pk), "path": request.path}


@login_required
@require_GET
def access_unavailable(request):
    return render(request, "tseh/access_unavailable.html", status=403)


def analysis_discount(request):
    previous = request.session.get("analysis_discount", "0")
    form = DiscountForm(request.GET if "discount" in request.GET else {"discount": previous})
    if form.is_valid():
        value = form.cleaned_data["discount"]
        if "discount" in request.GET:
            request.session["analysis_discount"] = str(value)
    else:
        value = Decimal(previous)
    return value, form


def price_assessment(dish, result, discount, settings):
    return assess(result.total, dish.base_price, discount, *effective_thresholds(dish, settings))


@require_GET
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse({"status": "ok"})
    except DatabaseError:
        return JsonResponse({"status": "unavailable"}, status=503)


@login_required
@require_GET
def dashboard(request):
    with read_snapshot():
        discount, discount_form = analysis_discount(request)
        graph, settings = catalog(), general_settings()
        list_form, params = parameters(request, "dishes")
        query = params.get("q", "")
        dishes = [dish for dish in Dish.objects.filter(status="active").prefetch_related("lines") if matches(query, dish.name)]
        uniqueness = uniqueness_report().dishes
        rows = []
        for dish in dishes:
            dish.uniqueness = uniqueness[dish.pk]
            result = graph.calculate(components(dish.lines.all()))
            rows.append((dish, result, price_assessment(dish, result, discount, settings)))
        zone = params.get("zone", "")
        if zone:
            rows = [row for row in rows if row[2].zone == zone]
        ranks = {"no_price": 0, "invalid": 1, "unconfigured": 2, "red": 3, "orange": 4, "green": 5}
        sort = params["sort"]
        key = {"attention": lambda row: (ranks[row[2].zone], row[0].name.casefold()),
            "name": lambda row: row[0].name.casefold(), "cost": lambda row: row[1].total,
            "price": lambda row: row[0].base_price, "ratio": lambda row: row[2].ratio}[sort.lstrip("-")]
        rows = sorted_rows(rows, key, lambda row: str(row[0].pk), sort.startswith("-"))
        listing = list_context(request, list_form, params, rows)
        return render(request, "tseh/dashboard.html", {**listing, "rows": listing["page"].object_list, "query": query, "active": "dishes",
            "dish_count": Dish.objects.filter(status="active").count(), "ingredient_count": Ingredient.objects.filter(status="active").count(), "supplier_count": Supplier.objects.filter(status="active").count(),
            "discount": discount, "discount_form": discount_form, "zone": zone})


@login_required
@require_GET
def suppliers(request):
    form, params = parameters(request, "suppliers")
    rows = [obj for obj in Supplier.objects.filter(status="active") if matches(params.get("q"), obj.name, obj.note)]
    rows = sorted_rows(rows, lambda obj: obj.name.casefold(), lambda obj: str(obj.pk), params["sort"] == "-name")
    listing = list_context(request, form, params, rows)
    return render(request, "tseh/suppliers.html", {**listing, "suppliers": listing["page"].object_list, "active": "suppliers"})


@login_required
@require_http_methods(["GET", "POST"])
def supplier_form(request, pk=None):
    if pk and get_object_or_404(Supplier, pk=pk).status == "trashed":
        return redirect("catalog-transition", kind="supplier", pk=pk, action="restore")
    if request.method == "POST":
        if pk:
            get_object_or_404(Supplier, pk=pk)
        form = SupplierForm(request.POST)
        if form.is_valid():
            try:
                commands.save_supplier(form.cleaned_data["token"], {key: form.cleaned_data[key] for key in ["name", "note"]}, pk)
                messages.success(request, "Поставщик сохранён.")
                return redirect("suppliers")
            except ValidationError as exc:
                add_error(form, exc)
        return render(request, "tseh/form.html", {"form": form, "title": "Изменить поставщика" if pk else "Добавить поставщика", "active": "suppliers", "back": "suppliers"}, status=400)
    with read_snapshot() as state:
        obj = get_object_or_404(Supplier, pk=pk) if pk else None
        initial = {"token": commands.stamp(state)}
        if obj:
            initial.update(name=obj.name, note=obj.note)
        return render(request, "tseh/form.html", {"form": SupplierForm(initial=initial), "title": "Изменить поставщика" if pk else "Добавить поставщика", "active": "suppliers", "back": "suppliers"})


@login_required
@require_GET
def ingredients(request):
    with read_snapshot():
        form, params = parameters(request, "ingredients")
        objects = Ingredient.objects.filter(status="active").select_related("current_price__supplier")
        if params.get("kind"):
            objects = objects.filter(kind=params["kind"])
        if params.get("unit"):
            objects = objects.filter(unit=params["unit"])
        if params.get("supplier"):
            objects = objects.filter(current_price__supplier_id=None if params["supplier"] == "none" else params["supplier"])
        rows = [(obj, obj.current_price.effective_unit_price)
                for obj in objects if matches(params.get("q"), obj.name)]
        sort = params["sort"]
        key = {"name": lambda row: row[0].name.casefold(), "price": lambda row: row[1], "loss": lambda row: row[0].loss}[sort.lstrip("-")]
        rows = sorted_rows(rows, key, lambda row: str(row[0].pk), sort.startswith("-"))
        listing = list_context(request, form, params, rows)
        return render(request, "tseh/ingredients.html", {**listing, "rows": listing["page"].object_list, "active": "ingredients"})


@login_required
@require_http_methods(["GET", "POST"])
def ingredient_create(request):
    if request.method == "POST":
        form = IngredientCreateForm(request.POST)
        if form.is_valid():
            data = {key: form.cleaned_data[key] for key in ["name", "kind", "loss", "unit", "pack_size", "pack_price", "price_date"]}
            supplier = form.cleaned_data["supplier"]
            data["supplier_id"] = supplier.pk if supplier else None
            try:
                pk = commands.create_ingredient(form.cleaned_data["token"], data,
                    confirm_zero=form.cleaned_data["confirm_zero"])
                messages.success(request, "Позиция сохранена. Первая цена стала действующей.")
                return redirect("ingredient-detail", pk=pk)
            except ValidationError as exc:
                add_error(form, exc)
        return render(request, "tseh/form.html", {"form": form, "title": "Добавить позицию", "active": "ingredients", "back": "ingredients"}, status=400)
    with read_snapshot() as state:
        return render(request, "tseh/form.html", {"form": IngredientCreateForm(initial={"token": commands.stamp(state)}),
            "title": "Добавить позицию", "intro": "Выберите ингредиент или упаковку, укажите фасовку и первую закупочную цену. Она сразу будет использоваться в калькуляциях.",
            "active": "ingredients", "back": "ingredients"})


@login_required
@require_GET
def ingredient_detail(request, pk):
    with read_snapshot():
        obj = get_object_or_404(Ingredient.objects.select_related("current_price__supplier"), pk=pk)
        graph = catalog()
        dishes = [dish for dish in Dish.objects.prefetch_related("lines") if obj.pk in graph.contributions(components(dish.lines.all()))] if request.access.dishes_view else []
        preparations = [prep for prep in Preparation.objects.all() if obj.pk in graph.leaf_costs[prep.pk]] if request.access.preparations_view else []
        return render(request, "tseh/ingredient_detail.html", {"obj": obj, "price": obj.current_price,
            "unit_price": obj.current_price.effective_unit_price, "dishes": dishes, "preparations": preparations, "active": "ingredients"})


@login_required
@require_http_methods(["GET", "POST"])
def ingredient_edit(request, pk):
    if get_object_or_404(Ingredient, pk=pk).status == "trashed":
        return redirect("catalog-transition", kind="ingredient", pk=pk, action="restore")
    if request.method == "POST":
        obj = get_object_or_404(Ingredient, pk=pk)
        form = IngredientEditForm(request.POST, ingredient=obj)
        if form.is_valid():
            try:
                commands.update_ingredient(form.cleaned_data["token"], pk, {key: form.cleaned_data[key] for key in ["name", "loss"]})
                messages.success(request, "Позиция сохранена. Зависимые калькуляции обновлены.")
                return redirect("ingredient-detail", pk=pk)
            except ValidationError as exc:
                add_error(form, exc)
        return render(request, "tseh/form.html", {"form": form, "title": "Изменить позицию", "active": "ingredients", "back": "ingredients"}, status=400)
    with read_snapshot() as state:
        obj = get_object_or_404(Ingredient, pk=pk)
        return render(request, "tseh/form.html", {"form": IngredientEditForm(ingredient=obj, initial={"token": commands.stamp(state), "name": obj.name, "loss": obj.loss}),
            "title": "Изменить позицию", "intro": "Для упаковки потери всегда равны 0%." if obj.kind == "packaging" else "Изменение потерь пересчитает все блюда, в которых используется этот ингредиент.",
            "active": "ingredients", "back": "ingredients"})


@login_required
@require_GET
def dish_detail(request, pk):
    with read_snapshot():
        obj = get_object_or_404(Dish, pk=pk)
        result, rows = dish_calculation(obj)
        report = uniqueness_report()
        uniqueness = report.dishes[obj.pk]
        ingredients_by_id = Ingredient.objects.in_bulk(uniqueness.ingredient_ids)
        for line, _ in rows:
            line.is_unique = line.ingredient_id in uniqueness.unique_ids
            if line.preparation_id:
                ingredient_ids = report.ingredient_ids_by_preparation[line.preparation_id]
                line.ingredient_breakdown = [
                    (ingredients_by_id[key], key in uniqueness.unique_ids)
                    for key in sorted(ingredient_ids, key=lambda key: (ingredients_by_id[key].name.casefold(), str(key)))
                ]
                line.unique_count = len(ingredient_ids & uniqueness.unique_ids)
        discount, discount_form = analysis_discount(request)
        return render(request, "tseh/dish_detail.html", {"obj": obj, "result": result, "rows": rows, "active": "dishes",
            "uniqueness": uniqueness,
            "discount": discount, "discount_form": discount_form, "assessment": price_assessment(obj, result, discount, general_settings()),
            **receipt_context(request)})


@login_required
@require_http_methods(["GET", "POST"])
def dish_form(request, pk=None):
    return recipe_form(request, pk, False)


@login_required
@require_http_methods(["GET", "POST"])
def preparation_form(request, pk=None):
    return recipe_form(request, pk, True)


def recipe_form(request, pk, is_preparation):
    model = Preparation if is_preparation else Dish
    form_class = PreparationForm if is_preparation else DishForm
    fields = ["name", "output_quantity", "output_unit"] if is_preparation else ["name", "base_price", "green_override", "orange_override"]
    back = "preparations" if is_preparation else "dashboard"
    command = commands.save_preparation if is_preparation else commands.save_dish
    detail = "preparation-detail" if is_preparation else "dish-detail"
    with read_snapshot() as state:
        obj = get_object_or_404(model, pk=pk) if pk else None
        if obj and is_preparation and obj.status == "trashed":
            return redirect("catalog-transition", kind="preparation", pk=pk, action="restore")
        if obj and not is_preparation and obj.status == "trashed":
            messages.info(request, "Блюдо в корзине. Восстановите его в архив перед редактированием.")
            return redirect("dish-detail", pk=obj.pk)
        discount, _ = analysis_discount(request)
        context = {"obj": obj, "active": "preparations" if is_preparation else "dishes", "back": back,
                   "is_preparation": is_preparation, "discount": discount, **draft_context(request, state)}
        if request.method == "GET":
            initial = {"token": commands.stamp(state)}
            line_initial = []
            if obj:
                initial.update({key: getattr(obj, key) for key in fields})
                if not is_preparation:
                    for name in ("green", "orange"):
                        value = getattr(obj, f"{name}_override")
                        initial[f"{name}_mode"] = "default" if value is None else "custom"
                        initial[f"{name}_value"] = value
                line_initial = [{"ingredient": line.ingredient_id, "preparation": line.preparation_id, "quantity": line.quantity, "unit": line.unit} for line in obj.lines.all()]
            context.update(form=form_class(initial=initial), formset=LineFormSet(initial=line_initial, form_kwargs={"preparation_recipe": is_preparation}), need_ingredient=not Ingredient.objects.filter(status="active").exists())
            return render(request, "tseh/recipe_editor.html", context)
        post = request.POST.copy()
        editing_lines = "remove_line" in post or "add_line" in post
        if editing_lines:
            # Button fallback without JavaScript: edit only the submitted draft, never the database.
            post.pop("preview_token", None)
            try:
                count = int(post.get("form-TOTAL_FORMS", ""))
                if not 0 <= count <= 100:
                    raise ValueError
                if "remove_line" in post:
                    index = int(post["remove_line"].removeprefix("form-"))
                    if not 0 <= index < count:
                        raise ValueError
                    post[f"form-{index}-DELETE"] = "on"
                    context["line_notice"] = "Компонент убран из состава. Изменения ещё не сохранены."
                else:
                    free = next((i for i in range(count) if post.get(f"form-{i}-DELETE") in ("on", "True", "true", "1")), None)
                    if free is None and count == 100:
                        context["line_notice"] = "Можно добавить не более 100 компонентов. Удалите строку, чтобы добавить другую."
                    else:
                        index = free if free is not None else count
                        for key in list(post):
                            if key.startswith(f"form-{index}-"):
                                del post[key]
                        post[f"form-{index}-unit"] = "g"
                        post[f"form-{index}-quantity_unit"] = "g"
                        post["form-TOTAL_FORMS"] = str(count + (free is None))
                        context["line_notice"] = "Добавлена строка компонента. Заполните её и пересчитайте."
            except (ValueError, TypeError):
                context["line_notice"] = "Не удалось изменить строки. Обновите форму."
        form = form_class(post)
        formset = LineFormSet(post, form_kwargs={"preparation_recipe": is_preparation})
        context.update(form=form, formset=formset)
        if editing_lines:
            return render(request, "tseh/recipe_editor.html", context)
        valid_form, valid_lines = form.is_valid(), formset.is_valid()
        if valid_form and valid_lines:
            data = {key: form.cleaned_data[key] for key in fields}
            clean = [f.cleaned_data for f in formset if f.cleaned_data and not f.cleaned_data.get("DELETE")]
            for row in clean:
                row["component"] = row.get("ingredient") or row.get("preparation")
            lines = [{"ingredient_id": row["ingredient"].pk if row.get("ingredient") else None,
                      "preparation_id": row["preparation"].pk if row.get("preparation") else None,
                      "quantity": row["quantity"], "unit": row["unit"]} for row in clean]
            payload = {"token": form.cleaned_data["token"], "id": pk, **data, "lines": lines, "discount": discount}
            try:
                base = signing.loads(form.cleaned_data["token"], salt=commands.SALT)
                if str(state.epoch) != base["epoch"] or state.revision != base["revision"]:
                    if request.POST.get("intent") == "save" and confirmed(request, payload):
                        result_id = command(form.cleaned_data["token"], data, lines, pk)
                        return saved_redirect(request, detail, pk=result_id)
                    raise commands.StaleWrite("Исходные данные изменились. Обновите страницу и повторите сравнение.")
                if request.POST.get("intent") == "save" and confirmed(request, payload):
                    result_id = command(form.cleaned_data["token"], data, lines, pk)
                    messages.success(request, "Заготовка сохранена. Зависимые калькуляции пересчитаны." if is_preparation else "Блюдо сохранено.")
                    return saved_redirect(request, detail, pk=result_id)
                old_graph = catalog()
                if is_preparation:
                    preview_key = pk or uuid.uuid4()
                    spec = Prep(data["output_quantity"], data["output_unit"], proposed_components(lines))
                    if obj and obj.unit != spec.unit and (obj.dish_uses.exists() or obj.preparation_uses.exists()):
                        raise ValidationError("Несовместимая смена единицы используемой заготовки запрещена.")
                    new_graph = catalog({preview_key: spec})
                    result = new_graph.prep_costs[preview_key]
                    context.update(preview_unit_price=new_graph.prep_prices[preview_key], output_unit=spec.unit)
                    if obj:
                        affected = old_graph.dependents(pk)
                        context["prep_impacts"] = [(prep, old_graph.prep_prices[prep.pk], new_graph.prep_prices[prep.pk], old_graph.preparations[prep.pk].unit, new_graph.preparations[prep.pk].unit) for prep in Preparation.objects.filter(pk__in=affected)]
                        dish_impacts = []
                        settings = general_settings()
                        for dish in Dish.objects.prefetch_related("lines"):
                            recipe = components(dish.lines.all())
                            if any(row.kind == "preparation" and row.key in affected for row in recipe):
                                before, after = old_graph.calculate(recipe), new_graph.calculate(recipe)
                                dish_impacts.append((dish, before, after, price_assessment(dish, before, discount, settings), price_assessment(dish, after, discount, settings)))
                        context["dish_impacts"] = dish_impacts if request.access.dishes_view else []
                else:
                    proposed = copy(obj) if obj else Dish()
                    commands.validate_dish_thresholds(proposed, data, is_new=not pk)
                    proposed.base_price = data["base_price"]
                    result = old_graph.calculate(proposed_components(lines))
                    context["assessment"] = price_assessment(proposed, result, discount, general_settings())
                context.update(preview=result, preview_rows=list(zip(clean, result.rows)),
                               preview_token=signing.dumps(preview_identity(request, payload), salt="preview"))
                if obj:
                    context["previous"] = old_graph.prep_costs[pk] if is_preparation else dish_calculation(obj, old_graph)[0]
                    context["previous_rows"] = list(zip(obj.lines.select_related("ingredient", "preparation"), context["previous"].rows))
                    context["difference"] = Fraction(result.total) - Fraction(context["previous"].total)
                return render(request, "tseh/recipe_editor.html", context)
            except (ValidationError, ValueError) as exc:
                add_error(form, exc if isinstance(exc, ValidationError) else ValidationError(str(exc)))
            except (signing.BadSignature, KeyError):
                add_error(form, ValidationError("Форма недействительна. Откройте её заново."))
        return render(request, "tseh/recipe_editor.html", context, status=400)


@login_required
@require_GET
def preparations(request):
    with read_snapshot():
        form, params = parameters(request, "preparations")
        graph = catalog()
        rows = [(obj, graph.prep_costs[obj.pk], graph.prep_prices[obj.pk]) for obj in Preparation.objects.filter(status="active")
                if matches(params.get("q"), obj.name) and (not params.get("unit") or obj.unit == params["unit"])]
        sort = params["sort"]
        key = {"name": lambda row: row[0].name.casefold(), "cost": lambda row: row[1].raw, "price": lambda row: row[2]}[sort.lstrip("-")]
        rows = sorted_rows(rows, key, lambda row: str(row[0].pk), sort.startswith("-"))
        listing = list_context(request, form, params, rows)
        return render(request, "tseh/preparations.html", {**listing, "rows": listing["page"].object_list, "active": "preparations"})


@login_required
@require_GET
def preparation_detail(request, pk):
    with read_snapshot():
        obj = get_object_or_404(Preparation, pk=pk)
        graph = catalog()
        result = graph.prep_costs[pk]
        affected = graph.dependents(pk)
        dishes = [dish for dish in Dish.objects.prefetch_related("lines") if any(row.kind == "preparation" and row.key in affected for row in components(dish.lines.all()))] if request.access.dishes_view else []
        return render(request, "tseh/preparation_detail.html", {"obj": obj, "result": result, "unit_price": graph.prep_prices[pk],
            "rows": list(zip(obj.lines.select_related("ingredient", "preparation"), result.rows)), "dishes": dishes,
            "dependents": Preparation.objects.filter(pk__in=affected).exclude(pk=pk), "active": "preparations",
            **receipt_context(request)})


@login_required
@require_http_methods(["GET", "POST"])
def thresholds(request):
    with read_snapshot() as state:
        settings = general_settings()
        if request.method == "POST":
            form = ThresholdForm(request.POST)
            if form.is_valid():
                try:
                    commands.save_thresholds(form.cleaned_data["token"], {key: form.cleaned_data[key] for key in ["green", "orange"]})
                    messages.success(request, "Общие пороги сохранены.")
                    return redirect("thresholds")
                except ValidationError as exc:
                    add_error(form, exc)
        else:
            form = ThresholdForm(initial={"token": commands.stamp(state), "green": settings.green, "orange": settings.orange})
        conflicts = [dish for dish in Dish.objects.all() if threshold_state(*effective_thresholds(dish, settings)) == "invalid"]
        over = any(value is not None and value > 100 for value in (settings.green, settings.orange))
        return render(request, "tseh/thresholds.html", {"form": form, "conflicts": conflicts, "over_hundred": over, "active": "thresholds"}, status=400 if request.method == "POST" else 200)


@login_required
@require_GET
def journal(request):
    form, params = parameters(request, "journal")
    entries = AuditEntry.objects.all()
    kind = params.get("kind", "")
    if kind:
        entries = entries.filter(object_type=kind)
    query = params.get("q", "")
    if params.get("from_date"):
        entries = entries.filter(at__date__gte=params["from_date"])
    if params.get("to_date"):
        entries = entries.filter(at__date__lte=params["to_date"])
    # Keep casefold semantics independent of the database collation. Scan only
    # small metadata for text search; large JSON snapshots are fetched per page.
    if query:
        ids = [pk for pk, name in entries.values_list("pk", "object_name").iterator()
               if matches(query, name)]
        entries = entries.filter(pk__in=ids)
    entries = entries.order_by("-at" if params["sort"] == "newest" else "at", "pk")
    listing = list_context(request, form, params, entries)
    return render(request, "tseh/journal.html", {**listing, "choices": KINDS, "kind": kind, "query": query, "active": "journal"})
