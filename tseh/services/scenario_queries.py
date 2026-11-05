"""Whole-assortment scenario comparison. Display rounding never changes graph math."""
from tseh.calculation.recipes import Item
from tseh.calculation.pricing import assess, effective_thresholds
from tseh.calculation.scenarios import compare_recipe
from tseh.models import Dish, Ingredient, Preparation
from .queries import catalog, components, general_settings


def scenario_impact(plan, discount=0):
    before = catalog()
    overrides = {key: Item(ingredient.unit, ingredient.loss, plan.prices[key].pack_price,
        plan.prices[key].pack_size, ingredient.kind, plan.prices[key].effective_unit_price)
        for key, ingredient in plan.ingredients.items()}
    after = catalog(item_override=overrides)
    settings = general_settings()
    ingredients = Ingredient.objects.in_bulk()
    preparations = {obj.pk: obj for obj in Preparation.objects.all()}
    sources = {obj.pk: obj for obj in plan.source_purchases}
    price_rows = []
    for row in plan.rows:
        key = row["ingredient_id"]
        ingredient = plan.ingredients[key]
        price_rows.append({"ingredient": ingredient, "before": ingredient.current_price,
            "after": plan.prices[key], "before_value": ingredient.current_price.effective_unit_price,
            "after_value": plan.prices[key].effective_unit_price, "is_new": key in plan.new_ingredient_ids,
            "purchase": sources.get(plan.prices[key].purchase_id)})
    def named_contributions(values):
        return [{"ingredient": ingredients[value.key], "value": value} for value in values]
    dishes = []
    for dish in Dish.objects.filter(status="active").prefetch_related("lines").order_by("name", "id"):
        lines = components(dish.lines.all())
        comparison = compare_recipe(before, after, lines)
        green, orange = effective_thresholds(dish, settings)
        branches = [{"component": (ingredients if value.component.kind == "ingredient" else preparations)[value.component.key],
            "value": value, "contributions": named_contributions(value.contributions)} for value in comparison.branches]
        dishes.append({"obj": dish, "before": comparison.before, "after": comparison.after,
            "old_assessment": assess(comparison.before.total, dish.base_price, discount, green, orange),
            "new_assessment": assess(comparison.after.total, dish.base_price, discount, green, orange),
            "comparison": comparison, "contributions": named_contributions(comparison.contributions),
            "branches": branches, "changed": comparison.before.raw != comparison.after.raw})
    affected_preparations = [{"obj": obj, "before": before.prep_prices[key], "after": after.prep_prices[key],
        "changed": before.prep_prices[key] != after.prep_prices[key], "unit": obj.unit}
        for key, obj in preparations.items() if set(plan.prices).intersection(before.leaf_costs[key])]
    return {"price_rows": price_rows, "preparations": affected_preparations, "dishes": dishes,
            "source_purchases": plan.source_purchases, "changed_count": sum(row["changed"] for row in dishes)}
