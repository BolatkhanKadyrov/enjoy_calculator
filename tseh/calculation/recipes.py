"""Acyclic recipes evaluated in topological order, without database access."""
from dataclasses import dataclass
from fractions import Fraction
from graphlib import CycleError, TopologicalSorter

from . import Calculation, FACTORS, line_cost, money, normalized


@dataclass(frozen=True)
class Item:
    unit: str
    loss: object
    pack_price: object
    pack_size: object
    kind: str = "ingredient"
    effective_price: object = None


@dataclass(frozen=True)
class Component:
    kind: str
    key: object
    quantity: object
    unit: str


@dataclass(frozen=True)
class Prep:
    output_quantity: object
    output_unit: str
    lines: tuple

    @property
    def unit(self):
        return FACTORS[self.output_unit][0]


class RecipeGraph:
    def __init__(self, items, preparations):
        self.items, self.preparations = items, preparations
        dependencies = {key: {row.key for row in prep.lines if row.kind == "preparation"} for key, prep in preparations.items()}
        if any(key not in preparations for children in dependencies.values() for key in children):
            raise ValueError("В рецептуре отсутствует связанная заготовка.")
        try:
            order = tuple(TopologicalSorter(dependencies).static_order())
        except CycleError:
            raise ValueError("Цикл в заготовках: заготовка не может зависеть от самой себя, в том числе через другие заготовки.")
        self.prep_costs, self.prep_prices, self.leaf_costs = {}, {}, {}
        for key in order:
            prep = preparations[key]
            if not prep.lines:
                raise ValueError("Состав заготовки не может быть пустым.")
            costs, leaves = self._rows(prep.lines, allow_packaging=False)
            output = normalized(prep.output_quantity, prep.output_unit, prep.unit)
            raw = sum(costs, Fraction())
            self.prep_costs[key] = Calculation(tuple(costs), raw, money(raw))
            self.prep_prices[key] = raw / output
            self.leaf_costs[key] = {item: cost / output for item, cost in leaves.items()}

    def _rows(self, rows, allow_packaging=True):
        costs, leaves = [], {}
        for row in rows:
            if row.kind == "ingredient":
                item = self.items.get(row.key)
                if item is None:
                    raise ValueError("Ингредиент рецептуры не найден.")
                if item.kind == "packaging" and (not allow_packaging or item.loss != 0):
                    raise ValueError("Упаковка добавляется в блюдо отдельно, с нулевыми потерями.")
                if item.effective_price is None:
                    cost = line_cost(row.quantity, row.unit, item.unit, item.loss, item.pack_price, item.pack_size)
                else:
                    if not 0 <= item.loss < 100:
                        raise ValueError("Потери должны быть от 0% до значения меньше 100%.")
                    cost = normalized(row.quantity, row.unit, item.unit) / (1 - Fraction(item.loss) / 100) * Fraction(item.effective_price)
                pieces = {row.key: cost}
            elif row.kind == "preparation":
                if row.key not in self.prep_prices:
                    raise ValueError("Заготовка рецептуры не найдена.")
                amount = normalized(row.quantity, row.unit, self.preparations[row.key].unit)
                cost = amount * self.prep_prices[row.key]
                pieces = {key: amount * value for key, value in self.leaf_costs[row.key].items()}
            else:
                raise ValueError("Неизвестный тип компонента.")
            costs.append(cost)
            for key, contribution in pieces.items():
                leaves[key] = leaves.get(key, Fraction()) + contribution
        return costs, leaves

    def calculate(self, rows):
        costs, _ = self._rows(rows)
        raw = sum(costs, Fraction())
        return Calculation(tuple(costs), raw, money(raw))

    def contributions(self, rows):
        return self._rows(rows)[1]

    def dependents(self, key):
        found = {key}
        while True:
            newer = found | {candidate for candidate, prep in self.preparations.items()
                            if any(line.kind == "preparation" and line.key in found for line in prep.lines)}
            if newer == found:
                return found
            found = newer
