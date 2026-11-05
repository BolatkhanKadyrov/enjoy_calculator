import pytest
from django.urls import reverse

from tseh.models import AuditEntry, Dish
from tests.test_component_picker import components, choice
from tests.test_web import get_token

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("page,title", [("dish-create", "Создать блюдо"), ("preparation-create", "Создать заготовку")])
def test_editor_starts_with_clear_title_and_single_component_choice(authenticated, components, page, title):
    response = authenticated.get(reverse(page))
    html = response.content.decode()
    assert f"<h1>{title}</h1>" in html
    assert 'data-draft-submitted="false"' in html
    assert 'name="form-0-component"' in html
    assert 'type="hidden" name="form-0-ingredient"' in html
    assert 'type="hidden" name="form-0-preparation"' in html
    assert "Обновить форму" not in html
    assert '<details class="reset-action">' in html and "Несохранённые изменения будут потеряны" in html
    assert 'id="reset-cancel"' in html and "Продолжить редактирование" in html


@pytest.mark.parametrize("valid", [True, False])
def test_preview_and_validation_response_keep_unsaved_state(authenticated, components, valid):
    data = {"token": get_token(authenticated, "dish-create"), "name": "Черновик", "base_price": "1000",
            "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "0", "form-0-component": choice(components[0]),
            "form-0-quantity": "100" if valid else "bad", "form-0-unit": "g", "intent": "preview"}
    before = AuditEntry.objects.count()
    response = authenticated.post(reverse("dish-create"), data)
    assert response.status_code == (200 if valid else 400)
    assert 'data-draft-submitted="true"' in response.content.decode()
    assert not Dish.objects.exists() and AuditEntry.objects.count() == before
    # Reset is a fresh GET, not a form.reset() returning to this unsaved POST.
    reset = authenticated.get(reverse("dish-create"))
    assert reset.context["form"]["name"].value() in (None, "")
    assert 'data-draft-submitted="false"' in reset.content.decode()
    assert not Dish.objects.exists() and AuditEntry.objects.count() == before


def test_general_form_error_keeps_unsaved_state_and_reset_confirmation(authenticated, state):
    data = {"token": get_token(authenticated, "supplier-create"), "name": "", "note": "Сохранить введённый текст"}
    response = authenticated.post(reverse("supplier-create"), data)
    assert response.status_code == 400
    assert 'data-draft-submitted="true"' in response.content.decode()
    assert response.context["form"]["note"].value() == data["note"]
    assert '<details class="reset-action">' in response.content.decode()
