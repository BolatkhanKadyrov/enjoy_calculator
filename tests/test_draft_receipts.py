from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.core import signing
from django.urls import reverse

from tseh.drafts import RECEIPT_MAX_AGE, RECEIPT_SALT
from tseh.models import AuditEntry, Dish, DomainState, Preparation
from tseh.services.commands import save_supplier
from tests.test_stage2_services import item, token

pytestmark = pytest.mark.django_db


def recipe(client, page):
    component = item()
    initial = client.get(reverse(page))
    return {"token": initial.context["form"].initial["token"], "name": "Подтверждение черновика",
            "base_price": "1000", "output_quantity": "1", "output_unit": "kg",
            "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "0",
            "form-0-component": f"ingredient:{component}", "form-0-quantity": "1", "form-0-unit": "kg",
            "intent": "preview", "_draft_request": str(uuid.uuid4())}


@pytest.mark.parametrize("page", ["dish-create", "preparation-create"])
def test_metadata_on_get_invalid_post_and_line_edit_is_read_only(authenticated, state, page):
    before = state.revision, AuditEntry.objects.count()
    url = reverse(page)
    get = authenticated.get(url)
    assert get.context["draft_revision"] == str(state.revision)
    assert get.context["draft_epoch"] == str(state.epoch)
    assert get.context["draft_user_scope"] == str(get.wsgi_request.user.pk)
    assert get.context["draft_request"] == ""
    marker = str(uuid.uuid4())
    for data in [{"_draft_request": marker}, {"_draft_request": marker, "add_line": "1", "form-TOTAL_FORMS": "0"}]:
        response = authenticated.post(url, data)
        assert response.context["draft_request"] == marker
        assert response.context["draft_revision"] == str(state.revision)
        assert response.context["draft_epoch"] == str(state.epoch)
        assert "draft_saved_receipt" not in response.context
        assert "Location" not in response
    state.refresh_from_db()
    assert (state.revision, AuditEntry.objects.count()) == before


@pytest.mark.parametrize("marker", ["", "not-a-uuid", "<script>bad</script>", "a" * 100])
def test_invalid_request_marker_is_not_echoed(authenticated, state, marker):
    response = authenticated.post(reverse("dish-create"), {"_draft_request": marker})
    assert response.context["draft_request"] == ""


@pytest.mark.parametrize("page,model", [("dish-create", Dish), ("preparation-create", Preparation)])
def test_success_receipt_and_idempotent_replay(authenticated, state, page, model):
    url = reverse(page)
    data = recipe(authenticated, page)
    preview = authenticated.post(url, data)
    assert preview.status_code == 200 and "Location" not in preview
    assert "draft_saved_receipt" not in preview.context
    data.update(intent="save", preview_token=preview.context["preview_token"])
    saved = authenticated.post(url, data)
    assert saved.status_code == 302
    encoded = parse_qs(urlsplit(saved.url).query)["draft_saved"][0]
    expected = {"user": str(saved.wsgi_request.user.pk), "path": url, "request": data["_draft_request"]}
    assert signing.loads(encoded, salt=RECEIPT_SALT, max_age=RECEIPT_MAX_AGE) == expected
    detail = authenticated.get(saved.url)
    assert detail.context["draft_saved_receipt"] == expected
    logs = AuditEntry.objects.count()
    repeated = authenticated.post(url, data)
    assert repeated.status_code == 302
    assert authenticated.get(repeated.url).context["draft_saved_receipt"] == expected
    assert AuditEntry.objects.count() == logs and model.objects.count() == 1


@pytest.mark.parametrize("marker", [None, "invalid-request"])
def test_legacy_or_invalid_marker_keeps_ordinary_redirect(authenticated, state, marker):
    data = recipe(authenticated, "dish-create")
    if marker is None:
        data.pop("_draft_request")
    else:
        data["_draft_request"] = marker
    preview = authenticated.post(reverse("dish-create"), data)
    data.update(intent="save", preview_token=preview.context["preview_token"])
    saved = authenticated.post(reverse("dish-create"), data)
    assert saved.status_code == 302
    assert saved.url == reverse("dish-detail", kwargs={"pk": Dish.objects.get().pk})
    assert "draft_saved_receipt" not in authenticated.get(saved.url).context


@pytest.mark.parametrize("failure", ["invalid", "changed", "stale"])
def test_failed_or_unconfirmed_save_never_acknowledges(authenticated, state, failure):
    url = reverse("dish-create")
    data = recipe(authenticated, "dish-create")
    preview = authenticated.post(url, data)
    data.update(intent="save", preview_token=preview.context["preview_token"])
    if failure == "invalid":
        data["form-0-quantity"] = "not a quantity"
    elif failure == "changed":
        data["form-0-quantity"] = "2"
    else:
        save_supplier(token(), {"name": "Параллельная правка", "note": ""})
    before = AuditEntry.objects.count(), DomainState.objects.get(pk=1).revision
    response = authenticated.post(url, data)
    assert response.status_code == (200 if failure == "changed" else 400)
    assert "Location" not in response and "draft_saved_receipt" not in response.context
    assert response.context["draft_request"] == data["_draft_request"]
    assert not Dish.objects.exists()
    assert (AuditEntry.objects.count(), DomainState.objects.get(pk=1).revision) == before


def test_detail_rejects_tampered_expired_wrong_account_and_wrong_shape(authenticated, state):
    data = recipe(authenticated, "dish-create")
    url = reverse("dish-create")
    preview = authenticated.post(url, data)
    data.update(intent="save", preview_token=preview.context["preview_token"])
    saved = authenticated.post(url, data)
    detail_url = urlsplit(saved.url).path
    encoded = parse_qs(urlsplit(saved.url).query)["draft_saved"][0]
    payload = signing.loads(encoded, salt=RECEIPT_SALT)
    wrong = [encoded + "x", "not-signed", signing.dumps(payload, salt="different-salt"),
             signing.dumps({**payload, "request": "not-uuid"}, salt=RECEIPT_SALT),
             signing.dumps({**payload, "path": "//external.invalid/"}, salt=RECEIPT_SALT),
             signing.dumps({**payload, "extra": "unexpected"}, salt=RECEIPT_SALT)]
    for receipt in wrong:
        response = authenticated.get(detail_url, {"draft_saved": receipt})
        assert response.status_code == 200 and "draft_saved_receipt" not in response.context
    import time
    with patch("django.core.signing.time.time", return_value=time.time() - RECEIPT_MAX_AGE - 10):
        expired = signing.dumps(payload, salt=RECEIPT_SALT)
    assert "draft_saved_receipt" not in authenticated.get(detail_url, {"draft_saved": expired}).context
    other = get_user_model().objects.create_user(username="other-team", password="test-only-password")
    authenticated.force_login(other)
    assert "draft_saved_receipt" not in authenticated.get(detail_url, {"draft_saved": encoded}).context
