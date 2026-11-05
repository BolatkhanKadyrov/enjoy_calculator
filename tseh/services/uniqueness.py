"""Structural uniqueness relative to the active assortment, independent of prices."""
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from graphlib import CycleError, TopologicalSorter
from uuid import UUID

from tseh.models import Dish, DishLine, Ingredient, Preparation, PreparationLine


@dataclass(frozen=True)
class DishUniqueness:
    ingredient_ids: frozenset[UUID]
    unique_ids: frozenset[UUID]

    @property
    def total_count(self):
        return len(self.ingredient_ids)

    @property
    def unique_count(self):
        return len(self.unique_ids)

    @property
    def percentage(self):
        return Fraction(100 * self.unique_count, self.total_count) if self.total_count else None


@dataclass(frozen=True)
class UniquenessReport:
    dishes: dict[UUID, DishUniqueness]
    ingredient_ids_by_preparation: dict[UUID, frozenset[UUID]]


def uniqueness_report():
    """Five bulk reads. Call within the caller's read snapshot for consistent UI data."""
    kinds = dict(Ingredient.objects.order_by().values_list("id", "kind"))
    preparation_ids = list(Preparation.objects.order_by().values_list("id", flat=True))
    prep_lines = list(PreparationLine.objects.order_by().values_list("parent_id", "ingredient_id", "preparation_id"))
    states = dict(Dish.objects.order_by().values_list("id", "status"))
    dish_ids = list(states)
    dish_lines = list(DishLine.objects.order_by().values_list("dish_id", "ingredient_id", "preparation_id"))

    direct = {key: set() for key in preparation_ids}
    dependencies = {key: set() for key in preparation_ids}

    def add_component(ingredients, preparations, ingredient_id, preparation_id):
        if (ingredient_id is None) == (preparation_id is None):
            raise ValueError("В строке состава должен быть ровно один компонент.")
        if ingredient_id is not None:
            if ingredient_id not in kinds:
                raise ValueError("Сырьё в составе не найдено.")
            if kinds[ingredient_id] == "ingredient":
                ingredients.add(ingredient_id)
            elif kinds[ingredient_id] != "packaging":
                raise ValueError("Неизвестный тип позиции в составе.")
        else:
            if preparation_id not in dependencies:
                raise ValueError("Заготовка в составе не найдена.")
            preparations.add(preparation_id)

    for parent, ingredient, preparation in prep_lines:
        if parent not in direct:
            raise ValueError("Родительская заготовка не найдена.")
        add_component(direct[parent], dependencies[parent], ingredient, preparation)
    try:
        order = tuple(TopologicalSorter(dependencies).static_order())
    except CycleError as exc:
        raise ValueError("Цикл в составе заготовок: уникальность не может быть рассчитана.") from exc
    expanded = {}
    for key in order:
        leaves = set(direct[key])
        for child in dependencies[key]:
            leaves.update(expanded[child])
        expanded[key] = frozenset(leaves)

    dish_ingredients = {key: set() for key in dish_ids}
    dish_preparations = {key: set() for key in dish_ids}
    for parent, ingredient, preparation in dish_lines:
        if parent not in dish_ingredients:
            raise ValueError("Блюдо для строки состава не найдено.")
        add_component(dish_ingredients[parent], dish_preparations[parent], ingredient, preparation)
    for key in dish_ids:
        for preparation in dish_preparations[key]:
            dish_ingredients[key].update(expanded[preparation])
    occurrences = Counter(ingredient for key, leaves in dish_ingredients.items() if states[key] == "active" for ingredient in leaves)
    dishes = {key: DishUniqueness(frozenset(leaves), frozenset(ingredient for ingredient in leaves
              if occurrences[ingredient] == (1 if states[key] == "active" else 0)))
              for key, leaves in dish_ingredients.items()}
    return UniquenessReport(dishes, expanded)


def dish_uniqueness():
    return uniqueness_report().dishes
