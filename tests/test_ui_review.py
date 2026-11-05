"""User-facing review regressions: reading order, tab titles and packaging edits."""
from decimal import Decimal as D
from html.parser import HTMLParser

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from tseh.models import AuditEntry, DomainState, Ingredient, PurchaseLine
from tseh.services.commands import update_ingredient
from tseh.services.purchases import save_delivery
from tests.test_price_web import preview as price_preview
from tests.test_purchase_web import (create_body, line_input, preview as purchase_preview,
                                     purchase)
from tests.test_stage2_services import item, token

pytestmark = pytest.mark.django_db


class PageOutline(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.form = None
        self.events = []
        self.in_title = False
        self.in_head = False
        self.title = ""
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "head":
            self.in_head = True
        if tag == "title" and self.in_head:
            self.in_title = True
        if tag == "form":
            assert self.form is None, "Nested forms change which fields a browser submits"
            self.form = attributes.get("id", "unnamed")
        if attributes.get("id") == "price-preview":
            self.events.append(("preview", self.form))
        if tag == "button" and attributes.get("value") == "confirm":
            self.events.append(("confirm", self.form))

    def handle_endtag(self, tag):
        if tag == "form":
            self.form = None
        elif tag == "title":
            self.in_title = False
        elif tag == "head":
            self.in_head = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


def assert_confirmation_follows_result(response, form_id):
    outline = PageOutline(response.content.decode())
    assert outline.events == [("preview", form_id), ("confirm", form_id)]


def test_confirm_after_result_in_same_form_for_prices_purchases_deletion_and_transfer(authenticated, state):
    key = item()
    response, _ = price_preview(authenticated, "create", key,
        pack_size="1", pack_price="2200", price_date="2026-11-04")
    assert_confirmation_follows_result(response, "price-form")
    response = purchase_preview(authenticated, "purchase-create",
        data=create_body(authenticated, line_input(key)))
    assert_confirmation_follows_result(response, "purchase-form")
    pk = purchase(line_input(key))
    row = PurchaseLine.objects.get(purchase_id=pk)
    response = purchase_preview(authenticated, "purchase-line-delete", pk, row.pk, delivery_action="keep")
    assert_confirmation_follows_result(response, "purchase-form")
    delivery = save_delivery(token(), {"amount": D(500), "note": ""})
    response = purchase_preview(authenticated, "delivery-attach", delivery,
        purchase=str(pk), confirm_addition="on")
    assert_confirmation_follows_result(response, "purchase-form")


def test_working_page_titles_identify_route_and_object_and_escape_names(authenticated, state):
    key = item()
    Ingredient.objects.filter(pk=key).update(name='Коробка </title><script>alert(1)</script>')
    routes = [("dashboard", [], "Блюда"), ("ingredients", [], "Сырьё и упаковка"),
        ("suppliers", [], "Поставщики"), ("preparations", [], "Заготовки"),
        ("journal", [], "Журнал"), ("thresholds", [], "Пороги зон"),
        ("purchase-list", [], "Закупки"), ("purchase-create", [], "Добавить закупку"),
        ("delivery-list", [], "Нераспределённая доставка"), ("delivery-create", [], "Добавить доставку"),
        ("price-list", [key], "Цены: Коробка"), ("price-create", [key], "Добавить цену: Коробка"),
        ("ingredient-edit", [key], "Изменить позицию: Коробка")]
    titles = []
    for route, args, expected in routes:
        response = authenticated.get(reverse(route, args=args))
        assert response.status_code == 200
        html = response.content.decode()
        title = PageOutline(html).title
        assert expected in title and title.endswith(" · Цех")
        assert '<script>alert(1)</script>' not in html
        titles.append(title)
    assert len(set(titles)) == len(titles)


def test_packaging_edit_has_fixed_zero_loss_and_rejects_forged_nonzero(authenticated, state):
    key = item(kind="packaging", unit="pc")
    response = authenticated.get(reverse("ingredient-edit", args=[key]))
    form = response.context["form"]
    assert response.context["title"] == "Изменить позицию"
    assert form.fields["name"].label == "Название позиции"
    assert form.fields["loss"].disabled
    assert "Для упаковки потери всегда равны 0%" in response.content.decode()
    body = {"token": form["token"].value(), "name": "Новая коробка"}
    before = (DomainState.objects.get(pk=1).revision, AuditEntry.objects.count())
    rejected = authenticated.post(reverse("ingredient-edit", args=[key]), {**body, "loss": "5"})
    assert rejected.status_code == 400
    assert (DomainState.objects.get(pk=1).revision, AuditEntry.objects.count()) == before
    assert "потери должны быть равны 0%" in rejected.content.decode()
    saved = authenticated.post(reverse("ingredient-edit", args=[key]), body)
    assert saved.status_code == 302
    obj = Ingredient.objects.get(pk=key)
    assert obj.name == "Новая коробка" and obj.loss == 0
    with pytest.raises(ValidationError):
        update_ingredient(token(), key, {"name": "Попытка обхода", "loss": D(1)})
    obj.refresh_from_db()
    assert obj.name == "Новая коробка" and obj.loss == 0


def test_ingredient_edit_keeps_loss_editable(authenticated, state):
    key = item(loss="25")
    response = authenticated.get(reverse("ingredient-edit", args=[key]))
    form = response.context["form"]
    assert not form.fields["loss"].disabled
    saved = authenticated.post(reverse("ingredient-edit", args=[key]),
        {"token": form["token"].value(), "name": "Сырьё", "loss": "20,25"})
    assert saved.status_code == 302
    assert Ingredient.objects.get(pk=key).loss == D("20.25")
