from decimal import Decimal as D
from fractions import Fraction as F
from html.parser import HTMLParser
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from tseh.models import AuditEntry, Dish, DomainState, Ingredient, Price, PriceApplication, Purchase, PurchaseLine
from tseh.services.commands import save_dish, update_ingredient
from tseh.services.prices import apply_price, create_price, delete_price
from tseh.services.queries import dish_calculation
from tests.test_price_web import SuccessfulControls
from tests.test_purchase_web import line_input, purchase
from tests.test_stage2_services import item, line, prep, token

pytestmark = pytest.mark.django_db


def controls(response):
    return SuccessfulControls(response.content.decode().replace('id="scenario-form"', 'id="price-form"')).values


def facts():
    return (DomainState.objects.get(pk=1).revision, AuditEntry.objects.count(), Price.objects.count(),
            PriceApplication.objects.count(), Purchase.objects.count(), PurchaseLine.objects.count())


def row(key, amount="2300", size="1", saved=None, **extra):
    return {"ingredient": str(key), "mode": "existing" if saved else "new", "saved_price": str(saved) if saved else "",
        "new-supplier": "", "new-price_date": "2026-11-04", "new-pack_size": size,
        "new-pack_price": amount, "new-comment": "", **extra}


def initial(client, rows=None, query=None):
    response = client.get(reverse("scenario-editor"), query or {})
    assert response.status_code == 200, response.content.decode()
    body = controls(response)
    if rows is not None:
        for key in list(body):
            if key.startswith("rows-") and key.split("-")[1].isdigit():
                del body[key]
        body["rows-TOTAL_FORMS"] = str(len(rows))
        for index, values in enumerate(rows):
            body.update({f"rows-{index}-{key}": value for key, value in values.items()})
    return {**body, "intent": "preview"}


def preview(client, body):
    before = facts()
    response = client.post(reverse("scenario-editor"), body)
    assert response.status_code == 200, response.content.decode()
    assert response.context["preview"]
    assert facts() == before
    return response


def confirm(client, body):
    response = preview(client, body)
    posted = {**controls(response), "intent": "confirm"}
    result = client.post(reverse("scenario-editor"), posted)
    assert result.status_code == 302, result.content.decode()
    return result, posted


def test_whole_assortment_269_to_306_then_apply_all_and_replay(authenticated, state):
    chicken, rice, oil, other = item(price="2000"), item(price="600"), item(price="900", unit="l"), item(price="50")
    dish = save_dish(token(), {"name": "Основное блюдо", "base_price": D(1000)},
        [line(chicken, "0.1"), line(rice, "0.1"), line(oil, "0.01", "l")])
    save_dish(token(), {"name": "Без изменений", "base_price": D(200)}, [line(other)])
    body = initial(authenticated, [row(chicken, "2300"), row(rice, "650"), row(oil, "1100")])
    response = preview(authenticated, body)
    dishes = {value["obj"].name: value for value in response.context["impact"]["dishes"]}
    comparison = dishes["Основное блюдо"]["comparison"]
    assert comparison.before.total == D(269) and comparison.after.total == D(306)
    assert comparison.delta == D(37) and comparison.percent == F(3700, 269)
    assert not dishes["Без изменений"]["changed"]
    html = response.content.decode()
    assert 'name="apply"' not in html and "Применить все цены" in html and "Детали: Основное блюдо" in html
    assert html.index('id="price-preview"') < html.index('value="confirm"') < html.rindex('</form>')
    saved, posted = confirm(authenticated, body)
    assert saved.url == reverse("dashboard")
    assert dish_calculation(Dish.objects.get(pk=dish))[0].total == D(306)
    before = facts()
    assert authenticated.post(reverse("scenario-editor"), posted).status_code == 302
    assert facts() == before
    assert AuditEntry.objects.filter(source="scenario").exists()
    assert "Посмотреть журнал" in authenticated.get(saved.url).content.decode()


def test_nested_repeated_leaf_is_aggregated_once_with_branch_explanation(authenticated, state):
    key = item(price="1000")
    Ingredient.objects.filter(pk=key).update(name="Мука")
    a = prep("Основа", [line(key, "0.2")])
    b = prep("Смесь", [line(key, "0.3"), line(a, "0.5", prep=True)])
    save_dish(token(), {"name": "Три ветви", "base_price": D(1000)},
        [line(key, "0.1"), line(b, "0.5", prep=True), line(a, "0.5", prep=True)])
    response = preview(authenticated, initial(authenticated, [row(key, "1200")]))
    data = response.context["impact"]["dishes"][0]
    assert data["comparison"].delta == D(80)
    assert len(data["contributions"]) == 1 and data["contributions"][0]["value"].raw_delta == F(80)
    assert [value["value"].raw_delta for value in data["branches"]] == [F(20), F(40), F(20)]
    assert "Это раскрытие тех же вкладов по ветвям, а не дополнительные слагаемые" in response.content.decode()


def test_remove_oil_after_preview_recalculates_304_and_applies_only_two_prices(authenticated, state):
    chicken, rice, oil = item(price="2000"), item(price="600"), item(price="900", unit="l")
    dish = save_dish(token(), {"name": "Удаление из сценария", "base_price": D(1000)},
        [line(chicken, "0.1"), line(rice, "0.1"), line(oil, "0.01", "l")])
    original_oil = Ingredient.objects.get(pk=oil).current_price_id
    response = preview(authenticated, initial(authenticated,
        [row(chicken, "2300"), row(rice, "650"), row(oil, "1100")]))
    removed = authenticated.post(reverse("scenario-editor"), {**controls(response), "remove_line": "2"})
    assert removed.status_code == 200 and "preview_token" not in controls(removed)
    body = {**controls(removed), "intent": "preview"}
    response = preview(authenticated, body)
    assert response.context["impact"]["dishes"][0]["comparison"].after.total == D(304)
    confirm(authenticated, body)
    assert dish_calculation(Dish.objects.get(pk=dish))[0].total == D(304)
    assert Ingredient.objects.get(pk=oil).current_price_id == original_oil
    assert Price.objects.filter(ingredient_id=oil).count() == 1


def test_inactive_saved_price_roundtrips_preview_and_confirmation(authenticated, state):
    key = item()
    confirm(authenticated, initial(authenticated, [row(key, **{"saved_price": "obsolete-choice"})]))
    assert Ingredient.objects.get(pk=key).current_price.pack_price == D(2300)


def test_sub_cent_change_has_explanation_even_when_adjustment_displays_zero(authenticated, state):
    a, b = item(price="1004", size="1000"), item(price="1004", size="1000")
    save_dish(token(), {"name": "Малое изменение", "base_price": D(100)}, [line(a), line(b)])
    response = preview(authenticated, initial(authenticated,
        [row(a, "1006", "1000"), row(b, "1006", "1000")]))
    comparison = response.context["impact"]["dishes"][0]["comparison"]
    assert comparison.before.total == comparison.after.total == D("2.01")
    assert comparison.display_adjustment == D(0) and comparison.raw_adjustment == F(-1, 250)
    html = response.content.decode()
    assert "Разница из-за округления" in html and "Изменения меньше 0,01 ₸" in html


def test_rounding_adjustment_and_zero_base_are_explicit(authenticated, state):
    a, b = item(price="1001", size="1000"), item(price="1001", size="1000")
    zero = item(price="0")
    save_dish(token(), {"name": "Округление", "base_price": D(100)}, [line(a), line(b)])
    save_dish(token(), {"name": "Нулевая база", "base_price": D(100)}, [line(zero)])
    response = preview(authenticated, initial(authenticated, [row(a, "1006", "1000"), row(b, "1006", "1000"), row(zero, "10")]))
    dishes = {value["obj"].name: value for value in response.context["impact"]["dishes"]}
    comparison = dishes["Округление"]["comparison"]
    assert comparison.delta == D("0.01") and comparison.display_adjustment == D("-0.01")
    assert [value.display_delta for value in comparison.contributions] == [D("0.01"), D("0.01")]
    assert dishes["Нулевая база"]["comparison"].percent is None
    html = response.content.decode()
    assert "Разница из-за округления" in html and "-0,01" in html and "Прежняя себестоимость — 0" in html
    assert 'title="Значение округлено"' in html


def test_purchase_preload_remove_preserves_full_composition_and_delivery(authenticated, state):
    a, b = item(), item()
    pk = purchase(line_input(a), line_input(b, size="10", count="2"))
    before_lines = list(PurchaseLine.objects.filter(purchase_id=pk).values())
    body = initial(authenticated, query={"purchase": str(pk)})
    assert body["rows-TOTAL_FORMS"] == "2"
    body["remove_line"] = "1"
    before = facts()
    removed = authenticated.post(reverse("scenario-editor"), body)
    assert removed.status_code == 200 and facts() == before
    assert "[]" not in removed.content.decode()
    body = {**controls(removed), "intent": "preview"}
    response = preview(authenticated, body)
    sources = response.context["plan"].source_purchases
    assert len(sources) == 1 and sources[0].lines.count() == 2
    assert {value.delivery_share for value in sources[0].lines.all()} == {D(2000), D(1000)}
    assert "Цена этой позиции не меняется" in response.content.decode()
    confirm(authenticated, body)
    assert list(PurchaseLine.objects.filter(purchase_id=pk).values()) == before_lines
    assert Purchase.objects.get(pk=pk).delivery_total == D(3000)


def test_nojs_add_configure_remove_keeps_inputs_and_never_applies(authenticated, state):
    a, b = item(), item()
    body = initial(authenticated, [row(a, "2222", **{"new-comment": "Точный ввод"})])
    before = facts()
    added = authenticated.post(reverse("scenario-editor"), {**body, "add_line": "1"})
    assert added.status_code == 200 and len(added.context["formset"]) == 2
    assert controls(added)["rows-0-new-pack_price"] == "2222"
    create_price(token(), b, {"supplier_id": None, "price_date": "2026-11-04", "pack_size": D(1),
        "pack_price": D(3000), "comment": "", "is_one_off": False})
    # The old command token remains intentionally old: configuring is read-only.
    body = {**controls(added), "rows-1-ingredient": str(b), "rows-1-mode": "existing", "configure_line": "1"}
    before = facts()
    configured = authenticated.post(reverse("scenario-editor"), body)
    assert configured.status_code == 200 and facts() == before
    assert len(configured.context["formset"][1].fields["saved_price"].choices) == 2
    assert not configured.context["formset"][1].new_price_form.errors
    assert controls(configured)["rows-0-new-comment"] == "Точный ввод"
    removed = authenticated.post(reverse("scenario-editor"), {**controls(configured), "remove_line": "1"})
    assert removed.status_code == 200 and facts() == before
    assert len(removed.context["formset"]) == 1


@pytest.mark.parametrize("change", [{"rows-0-new-pack_price": "9999"}, {"rows-0-new-is_one_off": "on"},
    {"rows-0-new-confirm_zero": "on"}, {"rows-0-mode": "existing"}, {"rows-TOTAL_FORMS": "0"}])
def test_changed_signed_body_requires_new_preview(authenticated, state, change):
    key = item()
    response = preview(authenticated, initial(authenticated, [row(key)]))
    before = facts()
    body = {**controls(response), "intent": "confirm", **change}
    assert authenticated.post(reverse("scenario-editor"), body).status_code == 400
    assert facts() == before


def test_discount_user_signature_and_changed_dependency_reject_confirmation(authenticated, state):
    key = item()
    response = preview(authenticated, initial(authenticated, [row(key)]))
    body = {**controls(response), "intent": "confirm"}
    before = facts()
    assert authenticated.post(reverse("scenario-editor"), {**body, "preview_token": body["preview_token"] + "x"}).status_code == 400
    authenticated.get(reverse("dashboard"), {"discount": "10"})
    assert authenticated.post(reverse("scenario-editor"), body).status_code == 400 and facts() == before
    response = preview(authenticated, initial(authenticated, [row(key)]))
    body = {**controls(response), "intent": "confirm"}
    other = get_user_model().objects.create_user(username="scenario-other", password="strong-test-password")
    authenticated.force_login(other)
    assert authenticated.post(reverse("scenario-editor"), body).status_code == 400 and facts() == before
    response = preview(authenticated, initial(authenticated, [row(key)]))
    body = {**controls(response), "intent": "confirm"}
    update_ingredient(token(), key, {"name": "Потери изменены", "loss": D(25)})
    before = facts()
    assert authenticated.post(reverse("scenario-editor"), body).status_code == 400 and facts() == before


def test_replay_precedes_lookup_of_deleted_selected_price(authenticated, state):
    key = item()
    original = Ingredient.objects.get(pk=key).current_price_id
    create_price(token(), key, {"supplier_id": None, "price_date": "2026-11-04", "pack_size": D(1),
        "pack_price": D(3000), "comment": "", "is_one_off": False})
    offer = Price.objects.filter(ingredient_id=key).exclude(pk=original).get()
    _, posted = confirm(authenticated, initial(authenticated, [row(key, saved=offer.pk)]))
    apply_price(token(), key, original)
    delete_price(token(), key, offer.pk)
    before = facts()
    assert authenticated.post(reverse("scenario-editor"), posted).status_code == 302
    assert facts() == before and Ingredient.objects.get(pk=key).current_price_id == original


def test_auth_csrf_get_readonly_and_bad_purchase_input(authenticated, user, state):
    anonymous = Client()
    path = reverse("scenario-editor")
    before = facts()
    assert anonymous.get(path).status_code == 302
    assert authenticated.get(path).status_code == 200 and facts() == before
    assert authenticated.get(path, {"purchase": "bad-uuid"}).status_code == 400
    assert authenticated.get(path, {"purchase": str(uuid.uuid4())}).status_code == 400
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    body = initial(strict)
    body["csrfmiddlewaretoken"] = "bad"
    assert strict.post(path, body).status_code == 403 and facts() == before


def test_hundred_rows_parse_and_render_over_thousand_controls_but_101_rejects(authenticated, state):
    key = item()
    rows = [row(key, **{"new-is_one_off": "on", "new-confirm_zero": "on"}) for _ in range(100)]
    body = initial(authenticated, rows)
    assert len(body) > 1000
    before = facts()
    response = authenticated.post(reverse("scenario-editor"), {**body, "configure_line": "99"})
    assert response.status_code == 200 and len(response.context["formset"]) == 100
    rendered = controls(response)
    assert len(rendered) > 1000 and "rows-99-new-pack_price" in rendered
    rejected = authenticated.post(reverse("scenario-editor"), {**body, "rows-TOTAL_FORMS": "101"})
    assert rejected.status_code == 400 and facts() == before
