from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from decimal import Decimal as D
from fractions import Fraction as F
from threading import Barrier
from unittest.mock import patch
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections

from tseh.models import AuditEntry, Dish, DomainState, Ingredient, Operation, Price, PriceApplication, Purchase
from tseh.services.commands import StaleWrite, save_dish, save_preparation, save_supplier, save_thresholds
from tseh.services.prices import create_price, delete_price
from tseh.services.purchases import save_purchase
from tseh.services.queries import dish_calculation
from tseh.services.scenarios import apply_scenario, prepare_scenario
from tseh.services.scenario_queries import scenario_impact
from tests.test_stage2_services import item, line, token

pytestmark = pytest.mark.django_db


def new_row(key, price, **extra):
    return {"ingredient_id": key, "data": {"supplier_id": None, "price_date": date(2026, 11, 4),
        "pack_size": D(1), "pack_price": D(price), "comment": "Сценарий", "is_one_off": False}, **extra}


def facts():
    return (DomainState.objects.get(pk=1).revision, Price.objects.count(), PriceApplication.objects.count(),
            AuditEntry.objects.count(), Operation.objects.count())


def sample():
    chicken, rice, oil = item(price="2000"), item(price="600"), item(price="900", unit="l")
    dish_id = save_dish(token(), {"name": "Контрольное блюдо", "base_price": D(1000)},
        [line(chicken, ".1"), line(rice, ".1"), line(oil, ".01", "l")])
    rows = [new_row(chicken, "2300"), new_row(rice, "650"), new_row(oil, "1100")]
    return Dish.objects.get(pk=dish_id), [chicken, rice, oil], rows


def test_combined_preview_269_to_306_removing_oil_304_and_all_dishes_readonly(state):
    dish, keys, rows = sample()
    unrelated = item(kind="packaging", price="25", unit="pc")
    other = save_dish(token(), {"name": "Без изменений", "base_price": D(100)}, [line(unrelated, "1", "pc")])
    save_thresholds(token(), {"green": D(30), "orange": D(40)})
    original, before = deepcopy(rows), facts()
    plan = prepare_scenario(rows)
    impact = scenario_impact(plan, D(10))
    assert rows == original and facts() == before
    assert [entry["ingredient"].pk for entry in impact["price_rows"]] == keys
    by_id = {entry["obj"].pk: entry for entry in impact["dishes"]}
    result = by_id[dish.pk]
    assert (result["before"].total, result["after"].total) == (D(269), D(306))
    assert result["comparison"].delta == 37 and result["comparison"].percent == F(3700, 269)
    assert result["old_assessment"].zone == "green" and result["new_assessment"].zone == "orange"
    assert result["new_assessment"].ratio == 34
    assert sorted(row["value"].display_delta for row in result["contributions"]) == [D(2), D(5), D(30)]
    assert all(row["ingredient"].name for row in result["contributions"])
    assert by_id[other]["changed"] is False and by_id[other]["after"].total == 25
    reduced = scenario_impact(prepare_scenario(rows[:2]))
    result = next(row for row in reduced["dishes"] if row["obj"].pk == dish.pk)
    assert result["after"].total == 304 and result["comparison"].delta == 35
    assert facts() == before and dish_calculation(dish)[0].total == 269


def test_apply_is_one_group_source_and_replay_binds_entire_body(state):
    dish, keys, rows = sample()
    stamp, before = token(), facts()
    group = apply_scenario(stamp, rows)
    assert isinstance(group, uuid.UUID) and Operation.objects.get(pk=group).result == group
    assert dish_calculation(dish)[0].total == 306
    assert Price.objects.count() == before[1] + 3 and PriceApplication.objects.count() == before[2] + 3
    logs = list(AuditEntry.objects.filter(group=group))
    assert len(logs) == 9 and {entry.source for entry in logs} == {"scenario"}
    assert {entry.object_type for entry in logs} == {"Price", "Ingredient", "PriceApplication"}
    after = facts()
    assert apply_scenario(stamp, rows) == group and facts() == after
    for changed in [rows[:2], list(reversed(rows)), [*rows[:2], new_row(keys[2], "1101")],
                    [{**rows[0], "confirm_zero": True}, *rows[1:]]]:
        with pytest.raises(StaleWrite):
            apply_scenario(stamp, changed)
        assert facts() == after
    supplier = save_supplier(token(), {"name": "После сценария", "note": ""})
    assert AuditEntry.objects.get(object_type="Supplier", object_id=str(supplier)).source == "manual"


def test_existing_purchase_price_selection_preserves_full_source_and_other_current_price(state):
    a, b = item(), item()
    source_id = save_purchase(token(), {"supplier_id": None, "purchase_date": date(2026, 11, 4), "delivery_total": D(3000)},
        [{"ingredient_id": a, "pack_size": D(5), "pack_price": D(5000), "pack_count": D(4)},
         {"ingredient_id": b, "pack_size": D(10), "pack_price": D(5000), "pack_count": D(2)}])
    source = Purchase.objects.get(pk=source_id)
    source_rows = list(source.lines.values())
    prices_before = list(source.prices.order_by("id").values())
    first, second = source.prices.get(ingredient_id=a), source.prices.get(ingredient_id=b)
    rows = [{"ingredient_id": str(a), "price_id": str(first.pk)}]
    plan = prepare_scenario(rows)
    assert plan.prices[a].effective_unit_price == 1100
    assert len(plan.source_purchases) == 1 and len(list(plan.source_purchases[0].lines.all())) == 2
    assert plan.source_purchases[0].selected_ingredient_ids == frozenset({a})
    before_b = Ingredient.objects.get(pk=b).current_price_id
    before = facts()
    apply_scenario(token(), rows)
    assert Ingredient.objects.get(pk=a).current_price_id == first.pk
    assert Ingredient.objects.get(pk=b).current_price_id == before_b != second.pk
    assert list(source.lines.values()) == source_rows
    assert list(source.prices.order_by("id").values()) == prices_before
    assert Price.objects.count() == before[1] and PriceApplication.objects.count() == before[2] + 1
    assert list(source.lines.values_list("delivery_share", flat=True)) == [D(2000), D(1000)]


def test_replay_happens_before_loading_a_now_deleted_selected_price(state):
    key = item()
    original = Ingredient.objects.get(pk=key).current_price_id
    create_price(token(), key, new_row(key, "2000")["data"])
    selected = Price.objects.filter(ingredient_id=key).exclude(pk=original).get()
    rows = [{"ingredient_id": key, "price_id": selected.pk}]
    stamp = token()
    group = apply_scenario(stamp, rows)
    delete_price(token(), key, selected.pk, replacement_price_id=original)
    after = facts()
    assert not Price.objects.filter(pk=selected.pk).exists()
    assert apply_scenario(stamp, rows) == group and facts() == after


def test_current_price_rejected_but_new_equal_value_is_an_explicit_new_offer(state):
    key = item()
    current = Ingredient.objects.get(pk=key).current_price_id
    before = facts()
    with pytest.raises(ValidationError, match="уже действует"):
        apply_scenario(token(), [{"ingredient_id": key, "price_id": current}])
    assert facts() == before
    rows = [new_row(key, "1000")]
    group = apply_scenario(token(), rows)
    assert Ingredient.objects.get(pk=key).current_price_id != current
    assert Ingredient.objects.get(pk=key).current_price.effective_unit_price == 1000
    assert AuditEntry.objects.filter(group=group).count() == 3


@pytest.mark.parametrize("bad", [[], None, [None], [{"ingredient_id": "invalid", "price_id": "invalid"}]])
def test_bad_scenario_structure_does_not_mutate(state, bad):
    before = facts()
    with pytest.raises(ValidationError):
        apply_scenario(token(), bad)
    assert facts() == before


def test_ownership_duplicate_modes_count_and_invalid_last_row_rejected_atomically(state):
    a, b = item(), item()
    own = Ingredient.objects.get(pk=a).current_price_id
    foreign = Ingredient.objects.get(pk=b).current_price_id
    good = new_row(a, "2000")
    invalids = [[good, good], [{"ingredient_id": a, "price_id": foreign}],
        [{"ingredient_id": a, "price_id": uuid.uuid4()}], [new_row(uuid.uuid4(), "1")],
        [{**good, "price_id": own}], [{"ingredient_id": a}], [{**good, "apply": False}],
        [{**good, "data": {**good["data"], "purchase_id": uuid.uuid4()}}],
        [good, new_row(b, "-1")], [good] * 101]
    before = facts()
    for rows in invalids:
        with pytest.raises(ValidationError):
            apply_scenario(token(), rows)
        assert facts() == before


@pytest.mark.parametrize("flag", [False, "on", 1, None])
def test_zero_requires_explicit_boolean_at_service_boundary(state, flag):
    key = item()
    before = facts()
    with pytest.raises(ValidationError):
        apply_scenario(token(), [new_row(key, "0", confirm_zero=flag)])
    assert facts() == before
    apply_scenario(token(), [new_row(key, "0", confirm_zero=True)])
    assert Ingredient.objects.get(pk=key).current_price.effective_unit_price == 0


def test_audit_failure_after_first_change_rolls_back_whole_scenario_and_resets_source(state):
    dish, keys, rows = sample()
    original = dict(Ingredient.objects.values_list("id", "current_price_id"))
    before = facts()
    from tseh.services.commands import audit
    calls = 0
    def fail_after_first_application(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise RuntimeError("audit unavailable after first position")
        return audit(*args, **kwargs)
    with patch("tseh.services.prices.audit", side_effect=fail_after_first_application):
        with pytest.raises(RuntimeError, match="after first"):
            apply_scenario(token(), rows)
    assert facts() == before
    assert dict(Ingredient.objects.values_list("id", "current_price_id")) == original
    assert dish_calculation(dish)[0].total == 269
    supplier = save_supplier(token(), {"name": "После ошибки", "note": ""})
    assert AuditEntry.objects.get(object_type="Supplier", object_id=str(supplier)).source == "manual"


def test_dependency_change_and_tampered_command_reject_without_partial_result(state):
    _, _, rows = sample()
    stamp = token()
    save_supplier(token(), {"name": "Параллельная правка", "note": ""})
    before = facts()
    with pytest.raises(StaleWrite):
        apply_scenario(stamp, rows)
    with pytest.raises(ValidationError, match="Форма"):
        apply_scenario(token() + "tampered", rows)
    assert facts() == before


def test_nested_contributions_are_named_and_query_count_does_not_grow_with_dishes(state, django_assert_num_queries):
    key = item()
    a = save_preparation(token(), {"name": "Заготовка A", "output_quantity": D(1), "output_unit": "kg"}, [line(key)])
    b = save_preparation(token(), {"name": "Заготовка B", "output_quantity": D(".5"), "output_unit": "kg"}, [line(a, ".5", prep=True)])
    for index in range(35):
        save_dish(token(), {"name": f"Блюдо {index:02}", "base_price": D(1000)},
                  [line(key, ".1"), line(a, ".2", prep=True), line(b, ".3", prep=True)])
    plan = prepare_scenario([new_row(key, "1200")])
    # Two catalog loads (3 each), settings, naming catalogs (2), dishes and lines (2).
    with django_assert_num_queries(11):
        impact = scenario_impact(plan)
    assert len(impact["dishes"]) == 35 and len(impact["preparations"]) == 2
    result = impact["dishes"][0]
    assert result["before"].total == 600 and result["after"].total == 720
    assert [branch["value"].display_delta for branch in result["branches"]] == [D(20), D(40), D(60)]
    assert len(result["contributions"]) == 1 and result["contributions"][0]["value"].display_delta == 120
    assert [branch["component"].name for branch in result["branches"]] == ["X", "Заготовка A", "Заготовка B"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("same_confirmation", [False, True])
def test_concurrent_scenarios_do_not_duplicate_partial_changes_or_audits(state, same_confirmation):
    key = item()
    rows = [new_row(key, "2000")]
    first = token()
    stamps = [first, first if same_confirmation else token()]
    before = facts()
    barrier = Barrier(2)
    def worker(index):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return apply_scenario(stamps[index], rows)
        except StaleWrite:
            return "stale"
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, [0, 1]))
    if same_confirmation:
        assert results[0] == results[1] and isinstance(results[0], uuid.UUID)
    else:
        assert sum(result == "stale" for result in results) == 1
    after = facts()
    assert after == (before[0] + 1, before[1] + 1, before[2] + 1, before[3] + 3, before[4] + 1)
    group = next(result for result in results if result != "stale")
    assert set(AuditEntry.objects.filter(group=group).values_list("source", flat=True)) == {"scenario"}
