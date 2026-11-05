from tseh.calculation.recipes import Component, Item, Prep, RecipeGraph
from tseh.models import GlobalSettings, Ingredient, Preparation, PreparationLine, SETTINGS_ID


def components(lines):
    return tuple(Component("ingredient" if line.ingredient_id else "preparation",
                           line.ingredient_id or line.preparation_id, line.quantity, line.unit) for line in lines)


def proposed_components(lines):
    return tuple(Component("ingredient" if row.get("ingredient_id") else "preparation",
                           row.get("ingredient_id") or row.get("preparation_id"), row["quantity"], row["unit"]) for row in lines)


def catalog(override=None, item_override=None):
    items = {obj.pk: Item(obj.unit, obj.loss, obj.current_price.pack_price, obj.current_price.pack_size, obj.kind, obj.current_price.effective_unit_price)
             for obj in Ingredient.objects.select_related("current_price")}
    if item_override:
        items.update(item_override)
    models = {obj.pk: obj for obj in Preparation.objects.all()}
    lines = {key: [] for key in models}
    for line in PreparationLine.objects.all():
        lines[line.parent_id].append(line)
    preparations = {key: Prep(obj.output_quantity, obj.output_unit, components(lines[key])) for key, obj in models.items()}
    if override:
        preparations.update(override)
    return RecipeGraph(items, preparations)


def dish_calculation(dish, graph=None):
    lines = list(dish.lines.select_related("ingredient__current_price", "preparation"))
    result = (graph or catalog()).calculate(components(lines))
    return result, list(zip(lines, result.rows))


def general_settings():
    return GlobalSettings.objects.filter(pk=SETTINGS_ID).first() or GlobalSettings()
