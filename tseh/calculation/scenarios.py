"""Exact price-scenario comparisons, without persistence or presentation state.

``compare_recipe(before_graph, after_graph, lines)`` compares the same recipe in
two prepared RecipeGraphs. Costs, deltas and percentages remain rational until
an explicit money boundary. The returned ``contributions`` are the additive
ingredient/packaging summary. ``branches`` explain those same contributions by
top-level recipe row; they must not be added to the summary a second time.
"""
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from . import Calculation, money
from .recipes import Component, RecipeGraph


@dataclass(frozen=True)
class LeafContribution:
    """Costs per dish portion for one ingredient UUID, including packaging."""

    key: object
    before: Fraction
    after: Fraction
    raw_delta: Fraction
    display_delta: Decimal

    @property
    def is_rounded(self):
        return self.raw_delta != Fraction(self.display_delta)


@dataclass(frozen=True)
class BranchContribution:
    """A top-level row and its flattened leaf costs, without path enumeration."""

    position: int
    component: Component
    before: Fraction
    after: Fraction
    raw_delta: Fraction
    display_delta: Decimal
    contributions: tuple[LeafContribution, ...]

    @property
    def is_rounded(self):
        return self.raw_delta != Fraction(self.display_delta)


@dataclass(frozen=True)
class ScenarioComparison:
    before: Calculation
    after: Calculation
    delta: Decimal
    percent: Fraction | None
    contributions: tuple[LeafContribution, ...]
    branches: tuple[BranchContribution, ...]
    display_adjustment: Decimal
    raw_adjustment: Fraction
    has_rounding: bool


def _leaf_contributions(before, after):
    result = []
    for key in sorted(before.keys() | after.keys(), key=str):
        old, new = before.get(key, Fraction()), after.get(key, Fraction())
        delta = new - old
        result.append(LeafContribution(key, old, new, delta, money(delta)))
    return tuple(result)


def compare_recipe(before_graph: RecipeGraph, after_graph: RecipeGraph, lines):
    """Return an immutable comparison of one unchanged recipe in two graphs.

    ``delta`` subtracts the final rounded costs, not the raw costs. ``percent``
    uses that delta and the final before cost; a zero base returns None.
    ``display_adjustment`` reconciles the displayed summary deltas with delta.
    ``raw_adjustment`` instead measures the rounding of the two final costs.
    ``has_rounding`` also notices rounded deltas that happen to cancel out.

    Leaves are ordered by their stable key; branches preserve recipe order.
    Unchanged leaves and branches remain available to the caller. Nested graph
    leaf maps are already aggregated, so shared subrecipes are never expanded
    into exponentially many paths. Work is bounded by top-level rows times the
    number of distinct leaf ingredients in their prepared graph maps.
    """
    lines = tuple(lines)
    before, after = before_graph.calculate(lines), after_graph.calculate(lines)
    old_leaves, new_leaves, branches = {}, {}, []
    for position, line in enumerate(lines):
        old = before_graph.contributions((line,))
        new = after_graph.contributions((line,))
        for key, value in old.items():
            old_leaves[key] = old_leaves.get(key, Fraction()) + value
        for key, value in new.items():
            new_leaves[key] = new_leaves.get(key, Fraction()) + value
        branch_delta = after.rows[position] - before.rows[position]
        branches.append(BranchContribution(
            position, line, before.rows[position], after.rows[position],
            branch_delta, money(branch_delta), _leaf_contributions(old, new),
        ))
    contributions = _leaf_contributions(old_leaves, new_leaves)
    exact_delta = Fraction(after.total) - Fraction(before.total)
    delta = money(exact_delta)
    percent = 100 * exact_delta / Fraction(before.total) if before.total else None
    raw_adjustment = exact_delta - sum((row.raw_delta for row in contributions), Fraction())
    display_adjustment = money(exact_delta - sum(
        (Fraction(row.display_delta) for row in contributions), Fraction()))
    has_rounding = bool(raw_adjustment or display_adjustment or
        any(row.is_rounded for row in contributions) or
        any(branch.is_rounded or any(row.is_rounded for row in branch.contributions)
            for branch in branches))
    return ScenarioComparison(before, after, delta, percent, contributions,
                              tuple(branches), display_adjustment, raw_adjustment, has_rounding)
