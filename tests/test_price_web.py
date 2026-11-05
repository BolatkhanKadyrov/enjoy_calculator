from decimal import Decimal as D
from fractions import Fraction as F
from html.parser import HTMLParser

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from tseh.models import AuditEntry, Dish, DomainState, Ingredient, Price, PriceApplication
from tseh.services.commands import save_dish, save_supplier
from tseh.services.queries import dish_calculation
from tests.test_stage2_services import item, line, prep, token

pytestmark = pytest.mark.django_db


class SuccessfulControls(HTMLParser):
    """Read the rendered price form as a browser would submit it (excluding the clicked button)."""
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.inside = False
        self.values = {}
        self.select = self.textarea = None
        self.feed(markup)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "form":
            self.inside = attrs.get("id") == "price-form"
        if not self.inside or "disabled" in attrs:
            return
        if tag == "input" and attrs.get("name"):
            kind = attrs.get("type", "text")
            if kind in {"submit", "button"} or (kind in {"checkbox", "radio"} and "checked" not in attrs):
                return
            self.values[attrs["name"]] = attrs.get("value", "on" if kind in {"checkbox", "radio"} else "")
        elif tag == "select":
            self.select = attrs.get("name")
        elif tag == "option" and self.select:
            if self.select not in self.values or "selected" in attrs:
                self.values[self.select] = attrs.get("value", "")
        elif tag == "textarea":
            self.textarea = attrs.get("name")
            if self.textarea:
                self.values[self.textarea] = ""

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "select":
            self.select = None
        elif tag == "textarea":
            # HTML's textarea rule ignores the first LF immediately after its start tag.
            if self.textarea and self.values[self.textarea].startswith("\n"):
                self.values[self.textarea] = self.values[self.textarea][1:]
            self.textarea = None

    def handle_data(self, data):
        if self.inside and self.textarea:
            self.values[self.textarea] += data


def url(action, key, price=None):
    kwargs = {"pk": key}
    if price is not None:
        kwargs["price_id"] = price
    return reverse(f"price-{action}", kwargs=kwargs)


def initial(client, action, key, price=None, **changes):
    response = client.get(url(action, key, price))
    assert response.status_code == 200
    data = SuccessfulControls(response.content.decode()).values
    data.update(changes)
    data["intent"] = "preview"
    return data


def preview(client, action, key, price=None, data=None, **changes):
    data = data or initial(client, action, key, price, **changes)
    response = client.post(url(action, key, price), data)
    assert response.status_code == 200, response.content.decode()
    assert response.context["preview"]
    return response, data


def confirm(client, action, key, price=None, data=None, **changes):
    response, sent = preview(client, action, key, price, data, **changes)
    # Use the actual rendered fields, not the original dict, to catch round-trip digest issues.
    body = SuccessfulControls(response.content.decode()).values
    body["intent"] = "confirm"
    saved = client.post(url(action, key, price), body)
    assert saved.status_code == 302, (saved.context["form"].errors if saved.context else saved.status_code)
    return saved, body


def offer(client, key, amount="2200", apply=False, **changes):
    before = set(Price.objects.values_list("pk", flat=True))
    confirm(client, "create", key, pack_size="1", pack_price=amount, price_date="2026-11-04",
            **({"apply_now": "on"} if apply else {}), **changes)
    return Price.objects.exclude(pk__in=before).get().pk


def state_facts():
    return (DomainState.objects.get(pk=1).revision, AuditEntry.objects.count(),
            Price.objects.count(), PriceApplication.objects.count())


def test_rendered_offer_roundtrip_escaping_oneoff_and_cascade_apply(authenticated, state):
    key = item(price="2000")
    first = Ingredient.objects.get(pk=key).current_price_id
    a = prep("A", [line(key)], output="2")
    b = prep("B", [line(a, "0.5", prep=True)], output="0.5")
    dish_id = save_dish(token(), {"name": "Блюдо", "base_price": D(500)}, [line(b, "100", "g", True)])
    second = offer(authenticated, key, comment='<script>alert("x")</script>', is_one_off="on")
    assert Ingredient.objects.get(pk=key).current_price_id == first
    assert PriceApplication.objects.count() == 1
    assert dish_calculation(Dish.objects.get(pk=dish_id))[0].total == D(100)
    listing = authenticated.get(url("list", key))
    assert '&lt;script&gt;' in listing.content.decode() and '<script>alert' not in listing.content.decode()
    assert "Разовая" in listing.content.decode()
    response, _ = preview(authenticated, "apply", key, second)
    assert response.context["impact"]["dishes"][0]["after"].total == D(110)
    saved, body = confirm(authenticated, "apply", key, second)
    assert dish_calculation(Dish.objects.get(pk=dish_id))[0].total == D(110)
    before = state_facts()
    assert authenticated.post(url("apply", key, second), body).status_code == 302
    assert state_facts() == before
    rejected = authenticated.post(url("apply", key, second), initial(authenticated, "apply", key, second))
    assert rejected.status_code == 400
    assert state_facts() == before


def test_correction_recomputes_all_history_and_current_impact(authenticated, state):
    key = item(price="2000")
    first = Ingredient.objects.get(pk=key).current_price_id
    second = offer(authenticated, key, apply=True)
    confirm(authenticated, "apply", key, first)
    ids_before = list(PriceApplication.objects.values_list("id", "applied_at"))
    confirm(authenticated, "correct", key, second, pack_price="2100", reason="Опечатка")
    history = authenticated.get(url("list", key)).context["history"]
    assert [row.unit_price for row in history] == [F(2000), F(2100), F(2000)]
    assert [row.change for row in history] == [None, F(5), F(-100, 21)]
    assert list(PriceApplication.objects.values_list("id", "applied_at")) == ids_before
    dish_id = save_dish(token(), {"name": "Блюдо", "base_price": D(500)}, [line(key, "0.1")])
    response, _ = preview(authenticated, "correct", key, first, pack_price="3000")
    assert response.context["impact"]["dishes"][0]["after"].total == D(300)
    confirm(authenticated, "correct", key, first, pack_price="3000")
    assert dish_calculation(Dish.objects.get(pk=dish_id))[0].total == D(300)


def test_delete_noncurrent_replay_and_history_rebase(authenticated, state):
    key = item(price="2000")
    second = offer(authenticated, key, apply=True)
    third = offer(authenticated, key, "2100", apply=True)
    saved, body = confirm(authenticated, "delete", key, second)
    history = authenticated.get(saved.url).context["history"]
    assert [row.unit_price for row in history] == [F(2000), F(2100)]
    assert history[1].change == F(5)
    before = state_facts()
    assert authenticated.post(url("delete", key, second), body).status_code == 302
    assert state_facts() == before
    assert Ingredient.objects.get(pk=key).current_price_id == third


@pytest.mark.parametrize("mode", ["existing", "new"])
def test_current_delete_with_zero_replacement(authenticated, state, mode):
    key = item()
    old = Ingredient.objects.get(pk=key).current_price_id
    if mode == "existing":
        replacement = offer(authenticated, key, "0", confirm_zero="on")
        changes = {"replacement_mode": "existing", "replacement_price": str(replacement)}
    else:
        changes = {"replacement_mode": "new", "replacement-pack_size": "1", "replacement-pack_price": "0",
                   "replacement-price_date": "2026-11-04", "replacement-confirm_zero": "on"}
        unconfirmed = initial(authenticated, "delete", key, old, **{**changes, "replacement-confirm_zero": ""})
        assert authenticated.post(url("delete", key, old), unconfirmed).status_code == 400
    confirm(authenticated, "delete", key, old, **changes)
    obj = Ingredient.objects.get(pk=key)
    assert obj.current_price.pack_price == 0 and obj.current_price_id != old
    assert not Price.objects.filter(pk=old).exists()
    assert PriceApplication.objects.count() == 1


@pytest.mark.parametrize("change", [{"pack_price": "3300"}, {"apply_now": "on"}, {"is_one_off": "on"}, {"confirm_zero": "on"}])
def test_changed_body_requires_new_preview_without_writes(authenticated, state, change):
    key = item()
    response, _ = preview(authenticated, "create", key, pack_size="1", pack_price="2200", price_date="2026-11-04")
    body = SuccessfulControls(response.content.decode()).values
    body.update(intent="confirm", **change)
    before = state_facts()
    result = authenticated.post(url("create", key), body)
    assert result.status_code == 400 and state_facts() == before


def test_changed_replacement_and_crossroute_token_never_write(authenticated, state):
    key = item()
    old = Ingredient.objects.get(pk=key).current_price_id
    a, b = offer(authenticated, key), offer(authenticated, key, "2300")
    response, _ = preview(authenticated, "delete", key, old, replacement_mode="existing", replacement_price=str(a))
    body = SuccessfulControls(response.content.decode()).values
    body.update(intent="confirm", replacement_price=str(b))
    before = state_facts()
    assert authenticated.post(url("delete", key, old), body).status_code == 400
    assert authenticated.post(url("apply", key, b), body).status_code == 400
    assert state_facts() == before


def test_stale_confirmation_does_not_leave_partial_data(authenticated, state):
    key = item()
    response, _ = preview(authenticated, "create", key, pack_size="1", pack_price="2200", price_date="2026-11-04", apply_now="on")
    body = SuccessfulControls(response.content.decode()).values
    body["intent"] = "confirm"
    save_supplier(token(), {"name": "Параллельное изменение", "note": ""})
    before = state_facts()
    assert authenticated.post(url("create", key), body).status_code == 400
    assert state_facts() == before


def test_preview_tampering_and_other_user_are_rejected(authenticated, state):
    key = item()
    response, _ = preview(authenticated, "create", key, pack_size="1", pack_price="2200", price_date="2026-11-04")
    body = SuccessfulControls(response.content.decode()).values
    body["intent"] = "confirm"
    before = state_facts()
    assert authenticated.post(url("create", key), {**body, "preview_token": body["preview_token"] + "x"}).status_code == 400
    other = get_user_model().objects.create_user(username="other-price-user", password="test-password")
    authenticated.force_login(other)
    assert authenticated.post(url("create", key), body).status_code == 400
    assert state_facts() == before


def test_auth_and_csrf_and_filtering_preserve_history(authenticated, client, user, state):
    key = item(price="2000")
    first = Ingredient.objects.get(pk=key).current_price_id
    offer(authenticated, key, apply=True)
    confirm(authenticated, "apply", key, first)
    anonymous = Client()
    for action in ["list", "create", "correct", "apply", "delete"]:
        path = url(action, key, first if action in {"correct", "apply", "delete"} else None)
        assert anonymous.get(path).status_code == 302
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    response = strict.get(url("create", key))
    body = SuccessfulControls(response.content.decode()).values
    body.update(pack_size="1", pack_price="2200", price_date="2026-11-04", intent="preview")
    body["csrfmiddlewaretoken"] = "bad"
    before = state_facts()
    assert strict.post(url("create", key), body).status_code == 403
    all_history = authenticated.get(url("list", key)).context["history"]
    filtered = authenticated.get(url("list", key), {"status": "unapplied", "q": "несуществующее", "sort": "-price"})
    assert filtered.status_code == 200 and not filtered.context["prices"]
    assert [(row.id, row.change) for row in filtered.context["history"]] == [(row.id, row.change) for row in all_history]
    assert all_history[2].change == F(-100, 11)
    assert state_facts() == before


def test_discount_changed_in_another_tab_requires_new_preview(authenticated, state):
    key = item()
    response, _ = preview(authenticated, "create", key, pack_size="1", pack_price="2200", price_date="2026-11-04", apply_now="on")
    body = SuccessfulControls(response.content.decode()).values
    body["intent"] = "confirm"
    assert authenticated.get(reverse("dashboard"), {"discount": "10"}).status_code == 200
    before = state_facts()
    rejected = authenticated.post(url("create", key), body)
    assert rejected.status_code == 400 and state_facts() == before
    assert rejected.context["discount"] == D(10)


def test_journal_shows_correction_metadata_and_deleted_application_amount(authenticated, state):
    key = item(price="2000")
    first = Ingredient.objects.get(pk=key).current_price_id
    second = offer(authenticated, key, apply=True, comment="Первоначальная пометка", is_one_off="on")
    confirm(authenticated, "apply", key, first)
    confirm(authenticated, "correct", key, second, pack_price="2100", comment="Проверенная пометка", reason="Исправлено по накладной")
    correction = authenticated.get(reverse("journal"), {"kind": "Price"}).content.decode()
    for text in ["Первоначальная пометка", "Проверенная пометка", "Разовая закупка", "Причина исправления", "Исправлено по накладной"]:
        assert text in correction
    confirm(authenticated, "delete", key, second)
    journal = authenticated.get(reverse("journal"), {"kind": "PriceApplication"})
    html = journal.content.decode()
    assert "Удалено" in html and "Запись цены" in html
    assert "2\u00a0100,00" in html and "Проверенная пометка" in html
    assert not Price.objects.filter(pk=second).exists()
    assert not PriceApplication.objects.filter(price_id=second).exists()
