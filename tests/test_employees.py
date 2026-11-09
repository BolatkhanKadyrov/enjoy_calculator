"""Account lifecycle, privilege boundaries and credential secrecy."""
import json

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse

from tseh.access import normalize_permissions
from tseh.employee_forms import EmployeeForm
from tseh.models.accounts import AccountAccess, AccountEvent
from tseh.models import AuditEntry, DomainState
from tseh.services.employees import employee_token, save_employee, disable_employee

pytestmark = pytest.mark.django_db
PASSWORD = "fresh-staff-secret-74!"
NEW_PASSWORD = "rotated-staff-secret-29!"


@pytest.fixture(autouse=True)
def cheap_passwords(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture
def owner():
    account = get_user_model().objects.create_user(username="account-owner", password="owner-secret-51!")
    AccountAccess.objects.create(user=account, is_owner=True)
    return account


@pytest.fixture
def owner_client(owner):
    client = Client()
    client.force_login(owner)
    return client


def create_staff(owner, username="cook-one", permissions=("dishes.view",)):
    return save_employee(owner, token=employee_token(owner), username=username, display_name="Повар",
                         permissions=permissions, password1=PASSWORD, password2=PASSWORD)


def edit_payload(owner, employee, **overrides):
    result = {"token": employee_token(owner, employee), "employee_id": employee.pk,
              "username": employee.username, "display_name": employee.first_name,
              "permissions": employee.account_access.permissions, "password1": "", "password2": ""}
    result.update(overrides)
    return result


def test_creation_normalizes_login_and_dependencies_without_privilege_escalation(owner, state):
    revision = state.revision
    employee = create_staff(owner, username="  CoOk.One  ", permissions=["dishes.add"])
    assert employee.username == "cook.one"
    assert employee.check_password(PASSWORD)
    assert employee.password != PASSWORD
    assert employee.is_active and not employee.is_staff and not employee.is_superuser
    assert not employee.account_access.is_owner
    assert employee.account_access.permissions == normalize_permissions(["dishes.add"])
    state.refresh_from_db()
    assert state.revision == revision
    assert not AuditEntry.objects.exists()
    assert AccountEvent.objects.get().action == "create"


@pytest.mark.parametrize("username", ["tseh", "TSEH_STAGING", "../admin", "абв", "ab", "a" * 151, "=evil"])
def test_reserved_or_invalid_login_rejected_without_partial_account(owner, username):
    before = get_user_model().objects.count()
    with pytest.raises(ValidationError):
        create_staff(owner, username=username)
    assert get_user_model().objects.count() == before
    assert not AccountEvent.objects.exists()


def test_logins_of_disabled_staff_remain_reserved_case_insensitively(owner):
    employee = create_staff(owner)
    disable_employee(owner, employee_id=employee.pk, token=employee_token(owner, employee, action="disable"))
    with pytest.raises(ValidationError):
        create_staff(owner, username="COOK-ONE")
    assert get_user_model().objects.filter(username="cook-one").count() == 1


def test_renamed_login_cannot_identify_a_different_employee(owner):
    employee = create_staff(owner)
    changed = save_employee(owner, **edit_payload(owner, employee, username="cook-renamed"))
    with pytest.raises(ValidationError, match="раньше использовался"):
        create_staff(owner, username="COOK-ONE")
    other = create_staff(owner, username="cook-two")
    with pytest.raises(ValidationError, match="раньше использовался"):
        save_employee(owner, **edit_payload(owner, other, username="cook-one"))
    changed.refresh_from_db()
    assert changed.username == "cook-renamed"
    assert not get_user_model().objects.filter(username="cook-one").exists()


def test_same_employee_may_return_to_their_historical_login(owner):
    employee = create_staff(owner)
    changed = save_employee(owner, **edit_payload(owner, employee, username="cook-renamed"))
    restored = save_employee(owner, **edit_payload(owner, changed, username="COOK-ONE"))
    assert restored.pk == employee.pk and restored.username == "cook-one"
    assert AccountEvent.objects.filter(target_name="cook-one").count() == 2


def test_historical_login_rejection_is_a_field_error_in_form(owner):
    employee = create_staff(owner)
    save_employee(owner, **edit_payload(owner, employee, username="cook-renamed"))
    form = EmployeeForm({"token": employee_token(owner), "username": "COOK-ONE",
        "password1": PASSWORD, "password2": PASSWORD}, actor=owner)
    assert not form.is_valid()
    assert "username" in form.errors
    assert "раньше использовался" in str(form.errors["username"])


@pytest.mark.parametrize("password1,password2", [("", ""), ("short", "short"), (PASSWORD, "different"),
                                               ("1234567890123", "1234567890123")])
def test_password_validation_before_create(owner, password1, password2):
    with pytest.raises(ValidationError):
        save_employee(owner, token=employee_token(owner), username="cook-one", permissions=[],
                      password1=password1, password2=password2)
    assert not get_user_model().objects.filter(username="cook-one").exists()
    assert not AccountEvent.objects.exists()


def test_blank_password_edit_keeps_hash_and_display_only_change_keeps_sessions(owner):
    employee = create_staff(owner)
    old_hash = employee.password
    browser = Client()
    browser.force_login(employee)
    session_key = browser.session.session_key
    changed = save_employee(owner, **edit_payload(owner, employee, display_name="Старший повар"))
    assert changed.password == old_hash
    assert changed.first_name == "Старший повар"
    assert Session.objects.filter(session_key=session_key).exists()


@pytest.mark.parametrize("change", ["password", "permissions", "username"])
def test_security_edits_revoke_only_target_sessions(owner, change):
    employee = create_staff(owner)
    other = create_staff(owner, "other-cook")
    browsers = [Client(), Client(), Client()]
    for browser, account in zip(browsers, [employee, employee, other]):
        browser.force_login(account)
    keys = [browser.session.session_key for browser in browsers]
    overrides = {"password": {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
                 "permissions": {"permissions": ["dishes.add"]}, "username": {"username": "new-cook"}}[change]
    changed = save_employee(owner, **edit_payload(owner, employee, **overrides))
    assert not Session.objects.filter(session_key__in=keys[:2]).exists()
    assert Session.objects.filter(session_key=keys[2]).exists()
    assert changed.account_access.version == 2
    if change == "password":
        assert changed.check_password(NEW_PASSWORD)
        assert not changed.check_password(PASSWORD)


def test_disable_preserves_history_and_account_and_revokes_sessions(owner):
    employee = create_staff(owner)
    browsers = [Client(), Client()]
    for browser in browsers:
        browser.force_login(employee)
    keys = [browser.session.session_key for browser in browsers]
    disabled = disable_employee(owner, employee_id=employee.pk,
                                token=employee_token(owner, employee, action="disable"))
    assert disabled.pk == employee.pk and not disabled.is_active
    assert not Session.objects.filter(session_key__in=keys).exists()
    assert list(AccountEvent.objects.order_by("pk").values_list("action", flat=True)) == ["create", "disable"]
    assert AccountEvent.objects.filter(target_name="cook-one").count() == 2
    assert not Client().login(username="cook-one", password=PASSWORD)


def test_stale_edit_and_disable_tokens_cannot_override_new_permissions(owner):
    employee = create_staff(owner)
    old_edit = edit_payload(owner, employee)
    old_disable = employee_token(owner, employee, action="disable")
    updated = save_employee(owner, **edit_payload(owner, employee, permissions=["preparations.view"]))
    with pytest.raises(ValidationError, match="устарела"):
        save_employee(owner, **old_edit)
    with pytest.raises(ValidationError, match="устарела"):
        disable_employee(owner, employee_id=employee.pk, token=old_disable)
    updated.refresh_from_db()
    assert updated.is_active
    assert updated.account_access.permissions == normalize_permissions(["preparations.view"])


def test_actor_target_and_action_bound_tokens(owner):
    employee = create_staff(owner)
    other = create_staff(owner, "cook-two")
    wrong_target = edit_payload(owner, employee, employee_id=other.pk)
    with pytest.raises(ValidationError):
        save_employee(owner, **wrong_target)
    with pytest.raises(ValidationError):
        disable_employee(owner, employee_id=employee.pk, token=employee_token(owner, employee))
    other_owner = get_user_model().objects.create_user(username="second-owner", password=PASSWORD)
    AccountAccess.objects.create(user=other_owner, is_owner=True)
    with pytest.raises(ValidationError):
        save_employee(other_owner, **edit_payload(owner, employee))


def test_expired_form_is_rejected_without_creating_employee(owner, monkeypatch):
    token = employee_token(owner)
    monkeypatch.setattr("tseh.services.employees.TOKEN_LIFETIME", -1)
    with pytest.raises(ValidationError, match="устарела"):
        save_employee(owner, token=token, username="expired-cook", permissions=[],
                      password1=PASSWORD, password2=PASSWORD)
    assert not get_user_model().objects.filter(username="expired-cook").exists()
    assert not AccountEvent.objects.exists()


def test_staff_cannot_create_edit_disable_or_escalate_owner(owner):
    employee = create_staff(owner)
    with pytest.raises(PermissionDenied):
        create_staff(employee, "another-cook")
    with pytest.raises(PermissionDenied):
        save_employee(employee, **edit_payload(employee, employee, permissions=["dishes.delete"]))
    with pytest.raises(PermissionDenied):
        disable_employee(employee, employee_id=owner.pk, token=employee_token(employee, owner, action="disable"))
    with pytest.raises(PermissionDenied):
        save_employee(owner, **edit_payload(owner, owner, username="renamed-owner"))
    with pytest.raises(PermissionDenied):
        disable_employee(owner, employee_id=owner.pk, token=employee_token(owner, owner, action="disable"))
    owner.refresh_from_db()
    assert owner.is_active and owner.username == "account-owner"


def test_unknown_permissions_are_rejected_even_through_service(owner):
    with pytest.raises(ValidationError):
        create_staff(owner, permissions=["employees.manage", "auth.add_user"])
    assert not get_user_model().objects.filter(username="cook-one").exists()


def test_account_events_never_contain_password_or_hash(owner):
    employee = create_staff(owner)
    original_hash = employee.password
    employee = save_employee(owner, **edit_payload(owner, employee, password1=NEW_PASSWORD, password2=NEW_PASSWORD))
    values = json.dumps(list(AccountEvent.objects.values("before", "after")), ensure_ascii=False)
    for secret in (PASSWORD, NEW_PASSWORD, original_hash, employee.password):
        assert secret not in values
    assert '"password_changed": true' in values


def test_http_create_ignores_privileged_fields_and_password_not_echoed(owner_client):
    response = owner_client.get(reverse("employee-create"))
    assert response.status_code == 200
    token = response.context["form"]["token"].value()
    response = owner_client.post(reverse("employee-create"), {"token": token, "username": "cook-new",
        "display_name": "Повар", "password1": PASSWORD, "password2": PASSWORD,
        "permissions": ["dishes.view"], "is_superuser": "1", "is_staff": "1", "is_owner": "1"})
    assert response.status_code == 302
    employee = get_user_model().objects.get(username="cook-new")
    assert not employee.is_superuser and not employee.is_staff and not employee.account_access.is_owner
    invalid = owner_client.post(reverse("employee-create"), {"token": token, "username": "another-cook",
        "password1": PASSWORD, "password2": "mismatch", "permissions": ["dishes.view"]})
    assert invalid.status_code == 400
    assert PASSWORD not in invalid.content.decode()
    assert 'value="mismatch"' not in invalid.content.decode()


def test_http_disable_requires_confirmation_and_get_never_changes_state(owner, owner_client):
    employee = create_staff(owner)
    path = reverse("employee-disable", args=[employee.pk])
    response = owner_client.get(path)
    assert response.status_code == 200
    token = response.context["form"]["token"].value()
    employee.refresh_from_db()
    assert employee.is_active
    response = owner_client.post(path, {"token": token})
    assert response.status_code == 400
    employee.refresh_from_db()
    assert employee.is_active
    response = owner_client.post(path, {"token": token, "confirm": "on"})
    assert response.status_code == 302
    employee.refresh_from_db()
    assert not employee.is_active
    inactive = owner_client.get(reverse("employee-list"), {"status": "inactive"})
    assert "cook-one" in inactive.content.decode()
    assert owner_client.get(reverse("employee-edit", args=[employee.pk])).status_code == 404


def test_staff_cannot_open_or_post_management_routes(owner):
    employee = create_staff(owner)
    client = Client()
    client.force_login(employee)
    for name, args in [("employee-list", []), ("employee-create", []),
                       ("employee-edit", [employee.pk]), ("employee-disable", [employee.pk])]:
        path = reverse(name, args=args)
        assert client.get(path).status_code == 403
        assert client.post(path, {"is_owner": "1", "confirm": "on"}).status_code == 403


def test_csrf_failure_never_creates_staff(owner):
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(owner)
    response = browser.post(reverse("employee-create"), {"token": employee_token(owner),
        "username": "csrf-cook", "password1": PASSWORD, "password2": PASSWORD})
    assert response.status_code == 403
    assert not get_user_model().objects.filter(username="csrf-cook").exists()
    assert not AccountEvent.objects.exists()


def test_form_groups_keep_selection_and_normalize_permissions(owner):
    form = EmployeeForm({"token": employee_token(owner), "username": "cook",
        "password1": PASSWORD, "password2": PASSWORD, "permissions": ["dishes.add"]}, actor=owner)
    assert form.is_valid(), form.errors
    assert form.cleaned_data["permissions"] == normalize_permissions(["dishes.add"])
    checked = {option["key"] for group in form.permission_groups for option in group["options"] if option["checked"]}
    assert checked == {"dishes.add"}
