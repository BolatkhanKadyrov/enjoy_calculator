from decimal import Decimal as D
from fractions import Fraction as F
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from tseh.models import (AuditEntry, Dish, DomainState, Ingredient, Price, PriceApplication,
                         Purchase, PurchaseLine, UnallocatedDelivery)
from tseh.services.commands import save_dish, save_supplier
from tseh.services.prices import apply_price
from tseh.services.purchases import save_delivery, save_purchase
from tseh.services.queries import dish_calculation
from tests.test_price_web import SuccessfulControls
from tests.test_stage2_services import item as base_item, line, token

pytestmark = pytest.mark.django_db


def item(name="X", **kwargs):
    key = base_item(**kwargs)
    Ingredient.objects.filter(pk=key).update(name=name)
    return key


def controls(response):
    return SuccessfulControls(response.content.decode().replace('id="purchase-form"', 'id="price-form"')).values


def facts():
    return (DomainState.objects.get(pk=1).revision, AuditEntry.objects.count(), Purchase.objects.count(),
            PurchaseLine.objects.count(), Price.objects.count(), PriceApplication.objects.count(),
            UnallocatedDelivery.objects.count())


def initial(client, route, *args, **changes):
    response = client.get(reverse(route, args=args))
    assert response.status_code == 200
    return {**controls(response), **changes, "intent": "preview"}


def preview(client, route, *args, data=None, **changes):
    body = data if data is not None else initial(client, route, *args, **changes)
    before = facts()
    response = client.post(reverse(route, args=args), body)
    assert response.status_code == 200, response.content.decode()
    assert response.context["preview"]
    assert facts() == before
    return response


def confirm(client, route, *args, data=None, **changes):
    response = preview(client, route, *args, data=data, **changes)
    body = {**controls(response), "intent": "confirm"}
    result = client.post(reverse(route, args=args), body)
    assert result.status_code == 302, result.content.decode()
    return result, body


def line_input(key, size="5", price="5000", count="4", **changes):
    return {"ingredient_id": key, "pack_size": D(size), "pack_price": D(price),
            "pack_count": D(count) if count is not None else None, "comment": "", "is_one_off": False, **changes}


def purchase(*rows, delivery="3000"):
    return save_purchase(token(), {"supplier_id": None, "purchase_date": "2026-11-04",
        "delivery_total": D(delivery), "comment": ""}, list(rows))


def create_body(client, *rows, delivery="3000"):
    body = initial(client, "purchase-create", purchase_date="2026-11-04", delivery_total=delivery)
    body["lines-TOTAL_FORMS"] = str(len(rows))
    for index, row in enumerate(rows):
        body[f"lines-{index}-id"] = ""
        for key, value in row.items():
            name = "ingredient" if key == "ingredient_id" else key
            if isinstance(value, bool):
                if value:
                    body[f"lines-{index}-{name}"] = "on"
            else:
                body[f"lines-{index}-{name}"] = "" if value is None else str(value)
    return body


def test_purchase_rendered_roundtrip_offers_allocations_and_idempotency(authenticated, state):
    chicken, rice = item("Курица", price="2000"), item("Рис", price="600")
    current = dict(Ingredient.objects.values_list("pk", "current_price_id"))
    body = create_body(authenticated, line_input(chicken), line_input(rice, size="10", count="2"))
    response = preview(authenticated, "purchase-create", data=body)
    assert [row.delivery_share for row in response.context["plan"].lines] == [D(2000), D(1000)]
    assert [row.effective_unit_price for row in response.context["plan"].lines] == [F(1100), F(550)]
    result, confirmation = confirm(authenticated, "purchase-create", data=body)
    assert dict(Ingredient.objects.values_list("pk", "current_price_id")) == current
    assert PriceApplication.objects.count() == 2
    saved = Purchase.objects.get()
    assert authenticated.get(result.url).status_code == 200
    assert {price.effective_unit_price for price in Price.objects.filter(purchase=saved)} == {F(1100), F(550)}
    before = facts()
    assert authenticated.post(reverse("purchase-create"), confirmation).status_code == 302
    assert facts() == before


def test_correct_purchase_shows_every_share_history_and_current_dish(authenticated, state):
    chicken, rice = item("Курица"), item("Масло", unit="l")
    pk = purchase(line_input(chicken), line_input(rice, size="10", count="2"))
    linked = Price.objects.get(purchase_id=pk, ingredient_id=chicken)
    apply_price(token(), chicken, linked.pk)
    dish = save_dish(token(), {"name": "Блюдо", "base_price": D(500)}, [line(chicken, "0.1")])
    body = initial(authenticated, "purchase-edit", pk, delivery_total="6000", reason="Исправление по чеку")
    response = preview(authenticated, "purchase-edit", pk, data=body)
    assert len(response.context["allocation_rows"]) == 2
    assert response.context["impact"]["dishes"][0]["after"].total == D(120)
    history = next(row for row in response.context["impact"]["histories"] if row["ingredient"].pk == chicken)
    assert history["before"][-1].unit_price == F(1100)
    assert history["after"][-1].unit_price == F(1200)
    assert "Товар, ₸/кг" in response.content.decode() and "Товар, ₸/л" in response.content.decode()
    before_events = list(PriceApplication.objects.values_list("pk", "applied_at"))
    confirm(authenticated, "purchase-edit", pk, data=body)
    assert list(PriceApplication.objects.values_list("pk", "applied_at")) == before_events
    assert dish_calculation(Dish.objects.get(pk=dish))[0].total == D(120)


@pytest.mark.parametrize("action,remaining,value", [("remove_all", "0", 1000), ("drop_share", "2000", 1100), ("reallocate", "3000", 1150)])
def test_three_line_deletion_choices(authenticated, state, action, remaining, value):
    a, b = item("A"), item("B")
    pk = purchase(line_input(a), line_input(b, size="10", count="2"))
    row = PurchaseLine.objects.get(purchase_id=pk, ingredient_id=b)
    response = authenticated.get(reverse("purchase-line-delete", args=[pk, row.pk]))
    assert "3\u00a0000,00" in response.content.decode() and "1\u00a0000,00" in response.content.decode()
    _, body = confirm(authenticated, "purchase-line-delete", pk, row.pk, delivery_action=action)
    assert Purchase.objects.get(pk=pk).delivery_total == D(remaining)
    assert Price.objects.get(purchase_id=pk, ingredient_id=a).effective_unit_price == value
    before = facts()
    assert authenticated.post(reverse("purchase-line-delete", args=[pk, row.pk]), body).status_code == 302
    assert facts() == before


@pytest.mark.parametrize("replacement", ["existing", "new"])
def test_last_line_delete_current_replacement_same_confirmation(authenticated, state, replacement):
    key = item()
    original = Ingredient.objects.get(pk=key).current_price_id
    pk = purchase(line_input(key))
    linked = Price.objects.get(purchase_id=pk)
    apply_price(token(), key, linked.pk)
    row = PurchaseLine.objects.get(purchase_id=pk)
    changes = {"delivery_action": "keep", "replacement_mode": replacement}
    if replacement == "existing":
        changes["replacement_price"] = str(original)
    else:
        changes.update({"replacement-pack_size": "1", "replacement-pack_price": "0",
                        "replacement-price_date": "2026-11-04", "replacement-confirm_zero": "on"})
    _, body = confirm(authenticated, "purchase-line-delete", pk, row.pk, **changes)
    assert not Purchase.objects.filter(pk=pk).exists()
    assert not Price.objects.filter(pk=linked.pk).exists()
    assert UnallocatedDelivery.objects.get().amount == D(3000)
    assert Ingredient.objects.get(pk=key).current_price_id != linked.pk
    before = facts()
    assert authenticated.post(reverse("purchase-line-delete", args=[pk, row.pk]), body).status_code == 302
    assert facts() == before


def test_repeated_ingredient_retains_current_price_when_one_line_deleted(authenticated, state):
    key = item()
    pk = purchase(line_input(key), line_input(key, size="2", price="2400", count="1"), delivery="0")
    linked = Price.objects.get(purchase_id=pk)
    apply_price(token(), key, linked.pk)
    row = PurchaseLine.objects.filter(purchase_id=pk).first()
    response = authenticated.get(reverse("purchase-line-delete", args=[pk, row.pk]))
    assert not response.context["needs_replacement"]
    confirm(authenticated, "purchase-line-delete", pk, row.pk, delivery_action="drop_share")
    assert Ingredient.objects.get(pk=key).current_price_id == linked.pk


def test_delivery_crud_reason_transfer_exact_sum_and_deleted_replay(authenticated, state):
    confirm(authenticated, "delivery-create", amount="3000,00", note='<script>alert("x")</script>')
    delivery = UnallocatedDelivery.objects.get()
    assert '&lt;script&gt;' in authenticated.get(reverse("delivery-list")).content.decode()
    bad = initial(authenticated, "delivery-edit", delivery.pk, amount="2900")
    assert authenticated.post(reverse("delivery-edit", args=[delivery.pk]), bad).status_code == 400
    confirm(authenticated, "delivery-edit", delivery.pk, amount="2900", reason="Ошибка по чеку")
    key = item()
    pk = purchase(line_input(key), delivery="600")
    before = facts()
    body = initial(authenticated, "delivery-attach", delivery.pk, purchase=str(pk))
    assert authenticated.post(reverse("delivery-attach", args=[delivery.pk]), body).status_code == 400
    assert facts() == before
    _, sent = confirm(authenticated, "delivery-attach", delivery.pk, purchase=str(pk), confirm_addition="on")
    assert Purchase.objects.get(pk=pk).delivery_total == D(3500)
    assert not UnallocatedDelivery.objects.filter(pk=delivery.pk).exists()
    before = facts()
    assert authenticated.post(reverse("delivery-attach", args=[delivery.pk]), sent).status_code == 302
    assert facts() == before
    other = save_delivery(token(), {"amount": D(20), "note": ""})
    _, body = confirm(authenticated, "delivery-delete", other)
    before = facts()
    assert authenticated.post(reverse("delivery-delete", args=[other]), body).status_code == 302
    assert facts() == before


def test_attach_missing_quantities_without_javascript(authenticated, state):
    key = item()
    pk = purchase(line_input(key, count=None), delivery="0")
    delivery = save_delivery(token(), {"amount": D(3000), "note": ""})
    body = initial(authenticated, "delivery-attach", delivery, purchase=str(pk))
    before = facts()
    incomplete = authenticated.post(reverse("delivery-attach", args=[delivery]), body)
    assert incomplete.status_code == 400 and facts() == before
    assert len(incomplete.context["quantity_forms"]) == 1
    assert "Для распределения доставки заполните" in incomplete.content.decode()
    body["intent"] = "select"
    before = facts()
    selected = authenticated.post(reverse("delivery-attach", args=[delivery]), body)
    assert selected.status_code == 200 and facts() == before
    body = controls(selected)
    body.update({"intent": "preview", "quantities-0-pack_count": "4"})
    confirm(authenticated, "delivery-attach", delivery, data=body)
    assert PurchaseLine.objects.get(purchase_id=pk).pack_count == D(4)
    assert Purchase.objects.get(pk=pk).delivery_total == D(3000)


def test_attach_purchase_options_distinguish_same_date_by_supplier_and_contents(authenticated, state):
    first, second = item("Курица"), item("Рис")
    a = purchase(line_input(first), delivery="600")
    b = purchase(line_input(second), delivery="600")
    supplier = save_supplier(token(), {"name": "Другой поставщик", "note": ""})
    Purchase.objects.filter(pk=b).update(supplier_id=supplier)
    delivery = save_delivery(token(), {"amount": D(3000), "note": ""})
    response = authenticated.get(reverse("delivery-attach", args=[delivery]))
    field = response.context["form"].fields["purchase"]
    choices = {str(value): label for value, label in field.choices}
    assert choices[str(a)] != choices[str(b)]
    assert "Курица" in choices[str(a)] and "Другой поставщик" in choices[str(b)] and "Рис" in choices[str(b)]


def test_add_remove_unsaved_rows_never_writes_or_implicitly_deletes_saved(authenticated, state):
    key = item()
    body = create_body(authenticated, line_input(key), delivery="0")
    body.update(add_line="1")
    before = facts()
    added = authenticated.post(reverse("purchase-create"), body)
    assert added.status_code == 200 and facts() == before
    assert "[]" not in added.content.decode()
    assert len(added.context["formset"]) == 2
    body = controls(added)
    body.update(remove_line="1")
    removed = authenticated.post(reverse("purchase-create"), body)
    assert removed.status_code == 200 and len(removed.context["formset"]) == 1 and facts() == before
    pk = purchase(line_input(key), delivery="0")
    body = initial(authenticated, "purchase-edit", pk)
    body["remove_line"] = "0"
    before = facts()
    rejected = authenticated.post(reverse("purchase-edit", args=[pk]), body)
    assert rejected.status_code == 400 and facts() == before
    body = initial(authenticated, "purchase-edit", pk)
    body["lines-0-id"] = str(uuid.uuid4())
    assert authenticated.post(reverse("purchase-edit", args=[pk]), body).status_code == 400
    assert facts() == before


@pytest.mark.parametrize("change", [{"delivery_total": "1"}, {"lines-0-pack_price": "6000"},
                                   {"lines-0-is_one_off": "on"}, {"confirm_zero": "on"}])
def test_changed_signed_purchase_body_never_writes(authenticated, state, change):
    key = item()
    response = preview(authenticated, "purchase-create", data=create_body(authenticated, line_input(key)))
    body = {**controls(response), **change, "intent": "confirm"}
    before = facts()
    assert authenticated.post(reverse("purchase-create"), body).status_code == 400
    assert facts() == before


def test_cross_user_route_discount_and_stale_confirm_rejected(authenticated, state):
    response = preview(authenticated, "delivery-create", amount="3000", note="")
    body = {**controls(response), "intent": "confirm"}
    before = facts()
    assert authenticated.post(reverse("purchase-create"), body).status_code == 400
    assert facts() == before
    authenticated.get(reverse("dashboard"), {"discount": "10"})
    assert authenticated.post(reverse("delivery-create"), body).status_code == 400
    assert facts() == before
    response = preview(authenticated, "delivery-create", amount="3000", note="")
    body = {**controls(response), "intent": "confirm"}
    other = get_user_model().objects.create_user(username="purchase-other", password="test-password")
    authenticated.force_login(other)
    assert authenticated.post(reverse("delivery-create"), body).status_code == 400
    assert facts() == before
    response = preview(authenticated, "delivery-create", amount="3000", note="")
    body = {**controls(response), "intent": "confirm"}
    save_supplier(token(), {"name": "Параллельное изменение", "note": ""})
    before = facts()
    assert authenticated.post(reverse("delivery-create"), body).status_code == 400
    assert facts() == before


def test_login_csrf_get_readonly_and_list_filters(authenticated, user, state):
    key = item("Курица")
    pk = purchase(line_input(key))
    delivery = save_delivery(token(), {"amount": D(3000), "note": "Проверка"})
    row = PurchaseLine.objects.get(purchase_id=pk)
    routes = [("purchase-list", []), ("purchase-create", []), ("purchase-detail", [pk]),
        ("purchase-edit", [pk]), ("purchase-line-delete", [pk, row.pk]), ("delivery-list", []),
        ("delivery-create", []), ("delivery-edit", [delivery]), ("delivery-delete", [delivery]),
        ("delivery-attach", [delivery])]
    anonymous = Client()
    before = facts()
    for route, args in routes:
        assert anonymous.get(reverse(route, args=args)).status_code == 302
        assert authenticated.get(reverse(route, args=args)).status_code == 200
    assert facts() == before
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    body = initial(strict, "delivery-create", amount="1", note="")
    body["csrfmiddlewaretoken"] = "bad"
    assert strict.post(reverse("delivery-create"), body).status_code == 403
    assert facts() == before
    filtered = authenticated.get(reverse("purchase-list"), {"q": "кУрИцА", "sort": "-delivery"})
    assert filtered.context["list_total"] == 1
    assert authenticated.get(reverse("delivery-list"), {"q": "нет"}).context["list_total"] == 0
