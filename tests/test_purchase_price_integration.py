"""Independent constants verify purchase prices survive every existing price UI."""
from decimal import Decimal as D
from fractions import Fraction as F

import pytest
from django.urls import reverse

from tseh.models import AuditEntry, Ingredient, Price, Purchase
from tseh.services.prices import apply_price
from tseh.services.purchases import save_purchase, save_delivery, attach_delivery
from tseh.templatetags.display import audit_rows, goods_unit, delivery_unit
from tests.test_stage2_services import item, token
from tests.test_purchase_commands import data, row

pytestmark = pytest.mark.django_db


def duplicate_price():
    key = item()
    purchase_id = save_purchase(token(), data("300"), [row(key, "1", "1000", "2"), row(key, "2", "3000", "4")])
    price = Price.objects.get(purchase_id=purchase_id)
    # (2000 + 12000 + 300) / (2 + 8) = 1430, independently specified.
    assert price.effective_unit_price == F(1430)
    assert goods_unit(price) == F(1400)
    assert delivery_unit(price) == F(30)
    return key, price


def test_duplicate_price_all_existing_pages_and_apply(authenticated, state):
    key, price = duplicate_price()
    apply_price(token(), key, price.pk)
    for name, kwargs in [("ingredients", {}), ("ingredient-detail", {"pk": key}),
                         ("price-list", {"pk": key}), ("journal", {}),
                         ("price-apply", {"pk": key, "price_id": price.pk})]:
        response = authenticated.get(reverse(name, kwargs=kwargs))
        assert response.status_code == 200
        if name != "journal":
            assert "1\xa0430,00" in response.content.decode()
    html = authenticated.get(reverse("price-list", kwargs={"pk": key})).content.decode()
    assert "1\xa0400,00" in html and "30,00" in html
    assert reverse("purchase-detail", kwargs={"pk": price.purchase_id}) in html
    for action, destination in [("correct", "purchase-edit"), ("delete", "purchase-detail")]:
        response = authenticated.get(reverse(f"price-{action}", kwargs={"pk": key, "price_id": price.pk}))
        assert response.status_code == 302
        assert response.url == reverse(destination, kwargs={"pk": price.purchase_id})


def test_journal_renders_purchase_lines_nullable_price_and_transfer(authenticated, state):
    key, price = duplicate_price()
    delivery_id = save_delivery(token(), {"amount": D("100"), "note": "Отдельный расход"})
    attach_delivery(token(), delivery_id, price.purchase_id, confirm_addition=True)
    response = authenticated.get(reverse("journal"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Перенесённая сумма" in html and "Количество упаковок" in html
    assert "Доставка до переноса" in html
    for event in AuditEntry.objects.all():
        audit_rows(event.before)
        audit_rows(event.after)


def test_single_purchase_keeps_pack_facts_and_zero_average_warns(authenticated, state):
    key = item()
    purchase_id = save_purchase(token(), data("3000"), [row(key, "5", "5000", "4")])
    price = Price.objects.get(purchase_id=purchase_id)
    apply_price(token(), key, price.pk)
    response = authenticated.get(reverse("price-list", kwargs={"pk": key}))
    html = response.content.decode()
    assert "5 кг" in html and "5\xa0000,00" in html
    zero_id = save_purchase(token(), data("0"), [row(key, "1", "0", "1"), row(key, "2", "0", "1")], confirm_zero=True)
    zero = Price.objects.get(purchase_id=zero_id)
    apply_price(token(), key, zero.pk)
    html = authenticated.get(reverse("ingredient-detail", kwargs={"pk": key})).content.decode()
    assert "Указана нулевая закупочная цена" in html
