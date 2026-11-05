from decimal import Decimal as D
from fractions import Fraction

import pytest
from django.urls import reverse

from tseh.models import AuditEntry, DomainState
from tseh.services.commands import save_dish
from tseh.templatetags.display import percentage
from tests.test_listing import raw
from tests.test_stage2_services import line, prep, token


pytestmark = pytest.mark.django_db


def test_detail_marks_raw_leaves_and_keeps_money_and_writes_unchanged(authenticated, state):
    common = raw("Мука", price="1000")
    direct = raw("Соль", price="0")
    nested = raw("Ягоды", price="100")
    pack = raw("Контейнер", price="10", unit="pc", kind="packaging")
    inner = prep("Начинка", [line(nested, "0.1")])
    outer = prep("Тесто", [line(inner, "0.5", prep=True), line(common, "0.1")])
    main = save_dish(token(), {"name": "Пирог", "base_price": D(1000)},
                     [line(outer, "1", prep=True), line(direct, "0.01"), line(pack, "1", unit="pc")])
    save_dish(token(), {"name": "Хлеб", "base_price": D(100)}, [line(common, "0.1")])
    before = DomainState.objects.get(pk=1).revision, AuditEntry.objects.count()
    response = authenticated.get(reverse("dish-detail", args=[main]))
    assert response.status_code == 200
    assert response.context["result"].total == D(115)  # 0.5 * (0.1 * 100) + 0.1 * 1000 + 10
    assert (response.context["uniqueness"].unique_count, response.context["uniqueness"].total_count) == (2, 3)
    rows = {row.ingredient_id or row.preparation_id: row for row, _ in response.context["rows"]}
    assert rows[direct].is_unique and not rows[pack].is_unique
    assert rows[outer].unique_count == 1
    assert {obj.pk: unique for obj, unique in rows[outer].ingredient_breakdown} == {common: False, nested: True}
    html = response.content.decode()
    assert "2/3 видов сырья" in html and "66,67%" in html
    assert html.count('class="uniqueness-badge"') == 2
    assert 'class="ingredient-breakdown"' in html and "только в этом блюде: 1" in html
    assert (DomainState.objects.get(pk=1).revision, AuditEntry.objects.count()) == before


def test_list_column_position_and_global_scope_under_filters_and_pagination(authenticated, state):
    common = raw("Общее сырьё")
    unique = raw("Особое сырьё")
    main = save_dish(token(), {"name": "А Особенное", "base_price": D(100)}, [line(common), line(unique)])
    for index in range(30):
        save_dish(token(), {"name": f"Б Другое {index:02}", "base_price": D(100)}, [line(common)])
    response = authenticated.get(reverse("dashboard"), {"q": "Особенное", "sort": "name"})
    assert len(response.context["rows"]) == 1
    assert response.context["rows"][0][0].pk == main
    assert response.context["rows"][0][0].uniqueness.percentage == 50
    html = response.content.decode()
    assert html.index(">Со скидкой</th>") < html.index(">Уникальность</th>") < html.index(">Доля</th>")
    assert '<td class="numeric">50%</td>' in html
    first_page = authenticated.get(reverse("dashboard"), {"sort": "name"})
    assert len(first_page.context["rows"]) == 30
    assert first_page.context["rows"][0][0].uniqueness.percentage == 50
    second_page = authenticated.get(reverse("dashboard"), {"sort": "name", "page": 2})
    assert len(second_page.context["rows"]) == 1
    assert second_page.context["rows"][0][0].uniqueness.percentage == 0


def test_packaging_only_has_no_percentage_or_unique_badge(authenticated, state):
    pack = raw("Контейнер", unit="pc", kind="packaging")
    key = save_dish(token(), {"name": "Упаковка", "base_price": D(100)}, [line(pack, unit="pc")])
    response = authenticated.get(reverse("dish-detail", args=[key]))
    html = response.content.decode()
    assert response.context["uniqueness"].percentage is None
    assert "<strong>—</strong>" in html and "0/0 видов сырья" in html
    assert 'class="uniqueness-badge"' not in html


def test_percentage_keeps_exact_counts_and_marks_only_display_rounding():
    assert percentage(None) == "—"
    assert percentage(Fraction(50)) == "50%"
    assert percentage(Fraction(0)) == "0%"
    assert percentage(Fraction(100)) == "100%"
    assert 'aria-label="Приблизительно"' in percentage(Fraction(100, 3))
    assert "33,33%" in percentage(Fraction(100, 3))
