"""Read-only effects of complete purchase plans, using one recipe graph per side."""
from fractions import Fraction

from tseh.calculation.pricing import assess, effective_thresholds
from tseh.calculation.recipes import Item
from tseh.models import Dish, Preparation, PriceApplication
from .price_queries import price_history
from .queries import catalog, components, general_settings


def purchase_impact(plan, discount=0):
    prices = dict(plan.prices)
    deleted = getattr(plan, "deleted_price", None)
    replacement = getattr(plan, "replacement_price", None)
    if replacement:
        prices[replacement.ingredient_id] = replacement
    affected = set(plan.old_prices) | set(prices)
    price_rows, overrides = [], {}
    selected = set(plan.apply_ingredient_ids)
    for key in sorted(affected, key=str):
        old, new = plan.old_prices.get(key), prices.get(key)
        ingredient = (new or old).ingredient
        current_changes = key in selected or (old is not None and ingredient.current_price_id == old.id)
        if new and current_changes:
            overrides[key] = Item(ingredient.unit, ingredient.loss, new.pack_price, new.pack_size,
                                  ingredient.kind, new.effective_unit_price)
        price_rows.append({"ingredient": ingredient, "before": old, "after": new,
            "before_value": old.effective_unit_price if old else None,
            "after_value": new.effective_unit_price if new else None, "changes_current": current_changes})
    before = catalog()
    after = catalog(item_override=overrides)
    preparations, dishes = [], []
    changed = set(overrides)
    for obj in Preparation.objects.order_by("name", "pk"):
        if changed.intersection(before.leaf_costs[obj.pk]):
            preparations.append({"obj": obj, "before": before.prep_prices[obj.pk],
                                 "after": after.prep_prices[obj.pk], "unit": obj.unit})
    settings = general_settings()
    for obj in Dish.objects.filter(status="active").prefetch_related("lines").order_by("name", "pk"):
        lines = components(obj.lines.all())
        if not changed.intersection(before.contributions(lines)):
            continue
        old, new = before.calculate(lines), after.calculate(lines)
        green, orange = effective_thresholds(obj, settings)
        dishes.append({"obj": obj, "before": old, "after": new,
                       "old_assessment": assess(old.total, obj.base_price, discount, green, orange),
                       "new_assessment": assess(new.total, obj.base_price, discount, green, orange)})
    events = list(PriceApplication.objects.filter(price__ingredient_id__in=affected)
                  .select_related("price__supplier").order_by("applied_at", "id"))
    grouped_events = {}
    for event in events:
        grouped_events.setdefault(event.price.ingredient_id, []).append(event)
    changed_prices = {price.id: price for price in plan.prices.values()}
    excluded = {deleted.id} if deleted else set()
    histories = []
    for row in price_rows:
        ingredient = row["ingredient"]
        history_events = grouped_events.get(ingredient.pk, ())
        histories.append({"ingredient": ingredient,
            "before": price_history(ingredient, events=history_events),
            "after": price_history(ingredient, overrides=changed_prices, excluded=excluded, events=history_events)})
    return {"preparations": preparations, "dishes": dishes, "histories": histories, "price_rows": price_rows}
