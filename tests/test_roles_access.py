"""Independent HTTP checks for role boundaries and explicit session resets.

These tests submit real signed forms, then change authorization state before
confirmation. Successful denials must preserve users, access and audit rows.
"""
from copy import deepcopy
import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.db.models import F
from django.test import Client
from django.urls import reverse

from tseh.access import Access, PERMISSIONS, normalize_permissions
from tseh.models.accounts import AccountAccess, AccountEvent
from tseh.services.employees import employee_token


pytestmark = pytest.mark.django_db
PASSWORD = 'roles-security-check-48!'
RESET_ROUTE = 'employee-reset-sessions'


@pytest.fixture(autouse=True)
def cheap_passwords(settings):
    settings.PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']


@pytest.fixture
def accounts(state):
    def create(role='employee', *, active=True, permissions=None):
        if permissions is None:
            permissions = ['dishes.view'] if role == 'employee' else []
        person = get_user_model().objects.create_user(
            username=role + '-' + uuid.uuid4().hex[:10], password=PASSWORD,
            is_active=active, is_staff=False, is_superuser=False,
        )
        AccountAccess.objects.create(user=person, is_owner=role == 'owner', is_admin=role == 'admin',
                                     permissions=normalize_permissions(permissions))
        browser = Client()
        browser.force_login(person)
        return person, browser

    owner, browser = create('owner')
    return create, owner, browser


def security_facts():
    return {
        'users': list(get_user_model().objects.order_by('pk').values()),
        'access': list(AccountAccess.objects.order_by('pk').values()),
        'events': list(AccountEvent.objects.order_by('pk').values()),
    }


def edit_body(response, target, **overrides):
    form = response.context['form']
    data = {
        'token': form.initial['token'], 'username': target.username,
        'display_name': target.first_name, 'permissions': target.account_access.permissions,
        'password1': '', 'password2': '',
    }
    if 'role' in form.fields:
        data['role'] = target.account_access.role
    return {**data, **overrides}


def create_body(response, **overrides):
    form = response.context['form']
    data = {'token': form.initial['token'], 'username': 'new-' + uuid.uuid4().hex[:10],
            'display_name': 'Новый сотрудник', 'permissions': ['dishes.view'],
            'password1': PASSWORD, 'password2': PASSWORD}
    if 'role' in form.fields:
        data['role'] = 'employee'
    return {**data, **overrides}


@pytest.mark.parametrize('target_role', ['owner', 'admin', 'self'])
@pytest.mark.parametrize('route', ['employee-edit', 'employee-disable', RESET_ROUTE])
def test_admin_cannot_open_or_post_management_of_protected_accounts(accounts, target_role, route):
    create, owner, _ = accounts
    administrator, browser = create('admin')
    target = owner if target_role == 'owner' else administrator if target_role == 'self' else create('admin')[0]
    target_url = reverse(route, args=[target.pk])
    before = security_facts()
    assert browser.get(target_url).status_code in (403, 404)
    action = {'employee-edit': 'save', 'employee-disable': 'disable', RESET_ROUTE: 'reset_sessions'}[route]
    # A valid signature does not replace the live actor/target role check.
    payload = {'token': employee_token(administrator, target, action=action), 'confirm': 'on',
               'username': target.username, 'permissions': ['dishes.view'],
               'password1': '', 'password2': ''}
    assert browser.post(target_url, payload).status_code in (403, 404)
    assert security_facts() == before


@pytest.mark.parametrize('injected_role', ['admin', 'owner', 'employee', 'invalid-role'])
def test_admin_cannot_submit_role_field_even_for_ordinary_employee(accounts, injected_role):
    create, _, _ = accounts
    _, browser = create('admin')
    target, _ = create()
    target_url = reverse('employee-edit', args=[target.pk])
    response = browser.get(target_url)
    assert response.status_code == 200
    assert 'role' not in response.context['form'].fields
    before = security_facts()
    assert browser.post(target_url, edit_body(response, target, role=injected_role)).status_code in (400, 403)
    assert security_facts() == before


@pytest.mark.parametrize('role', ['owner', 'unknown-role'])
def test_owner_cannot_create_owner_or_unrecognized_role(accounts, role):
    _, _, browser = accounts
    response = browser.get(reverse('employee-create'))
    before = security_facts()
    assert browser.post(reverse('employee-create'), create_body(response, role=role)).status_code in (400, 403)
    assert security_facts() == before


def test_admin_can_create_and_edit_ordinary_employee_without_role_escalation(accounts):
    create, _, _ = accounts
    _, browser = create('admin')
    response = browser.get(reverse('employee-create'))
    assert response.status_code == 200
    assert 'role' not in response.context['form'].fields
    data = create_body(response, is_admin='on', is_owner='on', is_superuser='on', is_staff='on')
    assert browser.post(reverse('employee-create'), data).status_code == 302
    target = get_user_model().objects.get(username=data['username'])
    assert target.account_access.role == 'employee'
    assert not target.is_staff and not target.is_superuser
    target_url = reverse('employee-edit', args=[target.pk])
    response = browser.get(target_url)
    assert browser.post(target_url, edit_body(response, target, display_name='Изменено администратором')).status_code == 302
    target.refresh_from_db()
    assert target.first_name == 'Изменено администратором'
    assert target.account_access.role == 'employee'


def test_admin_with_no_stored_permissions_has_every_business_permission_and_thresholds(accounts):
    create, _, _ = accounts
    administrator, browser = create('admin', permissions=[])
    assert administrator.account_access.permissions == []
    access = Access(administrator)
    assert len(PERMISSIONS) == 25
    assert all(access.can(key) for key in PERMISSIONS)
    assert access.can_manage_staff and not access.is_owner
    assert not access.can('unknown.permission')
    for route in ('dashboard', 'ingredients', 'suppliers', 'preparations', 'purchase-list',
                  'delivery-list', 'journal', 'thresholds', 'scenario-editor'):
        assert browser.get(reverse(route)).status_code == 200
    response = browser.get(reverse('thresholds'))
    assert browser.post(reverse('thresholds'), {'token': response.context['form'].initial['token'],
                                              'green': '35', 'orange': '55'}).status_code == 302
    from tseh.models import GlobalSettings
    thresholds = GlobalSettings.objects.get(pk=1)
    assert thresholds.green == 35 and thresholds.orange == 55


@pytest.mark.parametrize('route', ['exchange', 'export-json', 'export-xlsx',
                                 'restore-review', 'restore-download', 'restore-refresh'])
def test_admin_cannot_access_owner_backup_and_restore_routes(accounts, route):
    create, _, _ = accounts
    _, browser = create('admin', permissions=[])
    args = [uuid.UUID('cc03101c-83ea-44c5-9d98-787d249611b8')] if route.startswith('restore-') else None
    target_url = reverse(route, args=args)
    before = security_facts()
    assert browser.get(target_url).status_code == 403
    assert browser.post(target_url, {'confirm_download': 'on', 'review_token': 'forged'}).status_code == 403
    assert security_facts() == before


def test_admin_can_grant_all_valid_employee_rights_and_change_employee_password(accounts):
    create, _, _ = accounts
    _, browser = create('admin', permissions=[])
    response = browser.get(reverse('employee-create'))
    body = create_body(response, permissions=list(PERMISSIONS))
    assert browser.post(reverse('employee-create'), body).status_code == 302
    target = get_user_model().objects.get(username=body['username'])
    assert set(target.account_access.permissions) == set(PERMISSIONS)
    assert target.account_access.role == 'employee'
    target_browser = Client()
    target_browser.force_login(target)
    session_key = target_browser.session.session_key
    target_url = reverse('employee-edit', args=[target.pk])
    response = browser.get(target_url)
    changed_password = 'changed-by-admin-password-65!'
    assert browser.post(target_url, edit_body(response, target, password1=changed_password,
                                            password2=changed_password)).status_code == 302
    target.refresh_from_db()
    assert target.check_password(changed_password)
    assert set(target.account_access.permissions) == set(PERMISSIONS)
    assert not Session.objects.filter(session_key=session_key).exists()


def test_owner_can_promote_employee_but_old_admin_editor_cannot_touch_promoted_account(accounts):
    create, owner, owner_browser = accounts
    administrator, browser = create('admin')
    target, target_browser = create()
    target_url = reverse('employee-edit', args=[target.pk])
    stale = edit_body(browser.get(target_url), target, display_name='Forbidden stale update')
    session_key = target_browser.session.session_key
    response = owner_browser.get(target_url)
    assert owner_browser.post(target_url, edit_body(response, target, role='admin')).status_code == 302
    target.refresh_from_db()
    assert target.account_access.role == 'admin'
    assert target.account_access.permissions == []
    assert not Session.objects.filter(session_key=session_key).exists()
    before = security_facts()
    assert browser.post(target_url, stale).status_code in (403, 404)
    assert security_facts() == before


def test_owner_demotion_keeps_only_explicit_employee_rights_and_revokes_admin_sessions(accounts):
    create, _, owner_browser = accounts
    administrator, browser = create('admin')
    target_url = reverse('employee-edit', args=[administrator.pk])
    response = owner_browser.get(target_url)
    session_key = browser.session.session_key
    assert owner_browser.post(target_url, edit_body(response, administrator,
                             role='employee', permissions=[])).status_code == 302
    administrator.refresh_from_db()
    assert administrator.account_access.role == 'employee'
    assert administrator.account_access.permissions == []
    assert not Session.objects.filter(session_key=session_key).exists()
    access = Access(administrator)
    assert not access.can_manage_staff
    assert not any(access.can(key) for key in PERMISSIONS)


@pytest.mark.parametrize('action', ['edit', 'reset'])
def test_actor_version_rejects_old_admin_form_after_demotion_and_repromotion(accounts, action):
    create, _, _ = accounts
    administrator, browser = create('admin')
    target, _ = create()
    route = 'employee-edit' if action == 'edit' else RESET_ROUTE
    target_url = reverse(route, args=[target.pk])
    response = browser.get(target_url)
    assert response.status_code == 200
    body = edit_body(response, target, display_name='Forbidden old admin form') if action == 'edit' else {
        'token': response.context['form'].initial['token'], 'confirm': 'on'}
    # Preserve the old browser to isolate signed actor_version checking from
    # the separate session-revocation behavior of the administrative service.
    AccountAccess.objects.filter(user=administrator).update(is_admin=False, version=F('version') + 1)
    AccountAccess.objects.filter(user=administrator).update(is_admin=True, version=F('version') + 1)
    before = security_facts()
    assert browser.post(target_url, body).status_code in (400, 403)
    assert security_facts() == before


def test_demoted_admin_cannot_use_loaded_management_form(accounts):
    create, _, _ = accounts
    administrator, browser = create('admin')
    target, _ = create()
    target_url = reverse('employee-edit', args=[target.pk])
    body = edit_body(browser.get(target_url), target, display_name='Forbidden demoted update')
    AccountAccess.objects.filter(user=administrator).update(is_admin=False, version=F('version') + 1)
    before = security_facts()
    assert browser.post(target_url, body).status_code == 403
    assert security_facts() == before


@pytest.mark.parametrize('actor_role', ['owner', 'admin'])
def test_session_reset_is_explicit_target_only_and_preserves_account_data(accounts, actor_role):
    create, owner, owner_browser = accounts
    actor, browser = (owner, owner_browser) if actor_role == 'owner' else create('admin')
    target, target_browser = create()
    other, other_browser = create()
    second = Client()
    second.force_login(target)
    target_keys = [target_browser.session.session_key, second.session.session_key]
    unaffected_keys = [browser.session.session_key, other_browser.session.session_key]
    target_url = reverse(RESET_ROUTE, args=[target.pk])
    before = security_facts()
    response = browser.get(target_url)
    assert response.status_code == 200
    assert security_facts() == before
    assert Session.objects.filter(session_key__in=target_keys).count() == 2
    body = {'token': response.context['form'].initial['token'], 'confirm': 'on'}
    unchanged = {name: getattr(target, name) for name in ('username', 'password', 'is_active', 'is_staff', 'is_superuser')}
    profile_before = deepcopy(target.account_access.permissions)
    version = target.account_access.version
    assert browser.post(target_url, body).status_code == 302
    target.refresh_from_db()
    assert {name: getattr(target, name) for name in unchanged} == unchanged
    assert target.account_access.permissions == profile_before
    assert target.account_access.role == 'employee'
    assert target.account_access.version == version + 1
    assert not Session.objects.filter(session_key__in=target_keys).exists()
    assert Session.objects.filter(session_key__in=unaffected_keys).count() == 2
    entry = AccountEvent.objects.latest('pk')
    assert entry.action == 'reset_sessions'
    assert entry.actor_name == actor.username and entry.target_name == target.username
    encoded = json.dumps([entry.before, entry.after])
    assert PASSWORD not in encoded and unchanged['password'] not in encoded
    assert target_browser.get(reverse('dashboard')).status_code == 302
    before_replay = security_facts()
    assert browser.post(target_url, body).status_code in (400, 403)
    assert security_facts() == before_replay


def test_owner_self_reset_finishes_current_request_without_recreating_session(accounts):
    _, owner, browser = accounts
    second = Client()
    second.force_login(owner)
    keys = [browser.session.session_key, second.session.session_key]
    target_url = reverse(RESET_ROUTE, args=[owner.pk])
    response = browser.get(target_url)
    assert response.status_code == 200
    result = browser.post(target_url, {'token': response.context['form'].initial['token'], 'confirm': 'on'})
    assert result.status_code == 302 and result.url == reverse('login')
    assert not Session.objects.filter(session_key__in=keys).exists()
    assert browser.get(reverse('employee-list')).status_code == 302
    assert owner.account_access.is_owner


def test_reset_of_admin_invalidates_old_staff_confirmation_even_after_new_login(accounts):
    create, _, owner_browser = accounts
    administrator, browser = create('admin')
    target, _ = create()
    target_url = reverse('employee-edit', args=[target.pk])
    stale = edit_body(browser.get(target_url), target, display_name='Forbidden form from former session')
    reset_url = reverse(RESET_ROUTE, args=[administrator.pk])
    response = owner_browser.get(reset_url)
    assert response.status_code == 200
    assert owner_browser.post(reset_url, {'token': response.context['form'].initial['token'], 'confirm': 'on'}).status_code == 302
    administrator.refresh_from_db()
    browser.force_login(administrator)
    before = security_facts()
    assert browser.post(target_url, stale).status_code in (400, 403)
    assert security_facts() == before


def test_reset_confirmation_cannot_be_moved_to_another_target(accounts):
    create, _, browser = accounts
    first, first_browser = create()
    second, second_browser = create()
    response = browser.get(reverse(RESET_ROUTE, args=[first.pk]))
    before = security_facts()
    keys = [first_browser.session.session_key, second_browser.session.session_key]
    assert browser.post(reverse(RESET_ROUTE, args=[second.pk]),
                        {'token': response.context['form'].initial['token'], 'confirm': 'on'}).status_code in (400, 403)
    assert Session.objects.filter(session_key__in=keys).count() == 2
    assert security_facts() == before


@pytest.mark.parametrize('bad_action', ['save', 'disable'])
def test_session_reset_rejects_token_signed_for_another_action(accounts, bad_action):
    create, owner, browser = accounts
    target, target_browser = create()
    target_url = reverse(RESET_ROUTE, args=[target.pk])
    before = security_facts()
    session_key = target_browser.session.session_key
    response = browser.post(target_url, {'token': employee_token(owner, target, action=bad_action), 'confirm': 'on'})
    assert response.status_code in (400, 403)
    assert Session.objects.filter(session_key=session_key).exists()
    assert security_facts() == before


def test_session_reset_requires_confirmation_and_csrf(accounts):
    create, owner, _ = accounts
    target, target_browser = create()
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(owner)
    target_url = reverse(RESET_ROUTE, args=[target.pk])
    response = browser.get(target_url)
    body = {'token': response.context['form'].initial['token']}
    before = security_facts()
    assert browser.post(target_url, {**body, 'confirm': 'on'}).status_code == 403
    assert browser.post(target_url, {**body, 'csrfmiddlewaretoken': browser.cookies['csrftoken'].value}).status_code == 400
    assert Session.objects.filter(session_key=target_browser.session.session_key).exists()
    assert security_facts() == before


@pytest.mark.parametrize('target_kind', ['active', 'inactive', 'owner'])
def test_ordinary_employee_cannot_reset_any_sessions(accounts, target_kind):
    create, owner, _ = accounts
    actor, browser = create()
    target = owner if target_kind == 'owner' else create(active=target_kind == 'active')[0]
    target_url = reverse(RESET_ROUTE, args=[target.pk])
    before = security_facts()
    assert browser.get(target_url).status_code == 403
    assert browser.post(target_url, {'token': employee_token(actor, target, action='reset_sessions'), 'confirm': 'on'}).status_code == 403
    assert security_facts() == before


def test_admin_list_hides_protected_accounts_and_their_current_or_historical_events(accounts):
    create, owner, owner_browser = accounts
    administrator, browser = create('admin')
    protected, _ = create('admin')
    ordinary, _ = create()
    demoted, _ = create()
    def event(target, marker, before_role, after_role):
        AccountEvent.objects.create(actor_name=marker, target_name=target.username, action='update',
            before={'user_id': target.pk, 'role': before_role}, after={'user_id': target.pk, 'role': after_role})
    event(owner, 'SECRET-owner-history', 'owner', 'owner')
    event(protected, 'SECRET-admin-history', 'employee', 'employee')
    event(demoted, 'SECRET-demoted-history', 'admin', 'employee')
    event(ordinary, 'VISIBLE-ordinary-history', 'employee', 'employee')
    response = browser.get(reverse('employee-list'), {'status': 'all'})
    assert response.status_code == 200
    displayed = {row.pk for row in response.context['employees']}
    assert ordinary.pk in displayed and demoted.pk in displayed
    assert not {owner.pk, protected.pk, administrator.pk} & displayed
    content = response.content.decode()
    assert 'VISIBLE-ordinary-history' in content
    assert 'SECRET-' not in content
    owner_view = owner_browser.get(reverse('employee-list'), {'status': 'all'})
    assert 'SECRET-owner-history' in owner_view.content.decode()

