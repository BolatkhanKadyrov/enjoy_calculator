"""Read-only price history and exact previews; proposals never become history."""
from dataclasses import dataclass
from fractions import Fraction

from tseh.calculation import decimal_display, unit_price
from tseh.calculation.pricing import assess, effective_thresholds
from tseh.calculation.recipes import Item
from tseh.models import Dish, Preparation, PriceApplication
from .queries import catalog, components, general_settings


@dataclass(frozen=True)
class PriceHistoryRow:
    event: object
    id: int
    applied_at: object
    price: object
    unit_price: Fraction
    change: Fraction | None
    absolute_change: Fraction | None


def price_history(ingredient, overrides=None, excluded=None, *, events=None):
    overrides = overrides or {}
    excluded = set(excluded or ())
    rows, previous = [], None
    if events is None:
        events = PriceApplication.objects.filter(price__ingredient_id=ingredient.pk).select_related(
            "price__supplier").order_by("applied_at", "id")
    for event in events:
        if event.price_id in excluded:
            continue
        price = overrides.get(event.price_id, event.price)
        value = price.effective_unit_price
        absolute = None if previous is None else value - previous
        change = None if previous is None or previous == 0 else absolute / previous * 100
        rows.append(PriceHistoryRow(event, event.pk, event.applied_at, price, value, change, absolute))
        previous = value
    return rows


def history_chart(rows):
    """Bounded SVG coordinates; exact monetary values remain in rows/markers."""
    rows = list(rows)
    minimum = min((row.unit_price for row in rows), default=Fraction())
    maximum = max((row.unit_price for row in rows), default=Fraction())
    span = maximum - minimum
    markers = []
    for index, row in enumerate(rows):
        x = Fraction(330) if len(rows) == 1 else 40 + Fraction(580 * index, len(rows) - 1)
        y = Fraction(110) if not span else 200 - 180 * (row.unit_price - minimum) / span
        markers.append({"x": format(decimal_display(x), ".3f"),
                        "y": format(decimal_display(y), ".3f"), "event_id": row.id,
                        "applied_at": row.applied_at, "unit_price": row.unit_price})
    return {"view_box": "0 0 640 240", "points": " ".join(f"{p['x']},{p['y']}" for p in markers),
            "markers": markers, "minimum": minimum, "maximum": maximum,
            "x_label": "Последовательность применений"}


def price_impact(ingredient, proposed_price, discount):
    before = catalog()
    after = catalog(item_override={ingredient.pk: Item(ingredient.unit, ingredient.loss,
        proposed_price.pack_price, proposed_price.pack_size, ingredient.kind, proposed_price.effective_unit_price)})
    preparations = []
    for obj in Preparation.objects.order_by("name", "pk"):
        if ingredient.pk in before.leaf_costs[obj.pk]:
            preparations.append({"obj": obj, "before": before.prep_prices[obj.pk],
                                 "after": after.prep_prices[obj.pk], "unit": obj.unit})
    dishes = []
    settings = general_settings()
    for obj in Dish.objects.filter(status="active").prefetch_related("lines").order_by("name", "pk"):
        lines = components(obj.lines.all())
        if ingredient.pk not in before.contributions(lines):
            continue
        old, new = before.calculate(lines), after.calculate(lines)
        green, orange = effective_thresholds(obj, settings)
        dishes.append({"obj": obj, "before": old, "after": new,
                       "old_assessment": assess(old.total, obj.base_price, discount, green, orange),
                       "new_assessment": assess(new.total, obj.base_price, discount, green, orange)})
    return {"preparations": preparations, "dishes": dishes}
