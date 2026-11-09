"""Authorization regressions at the HTTP boundary, independent of UI controls.

The permission matrix deliberately uses nonexistent IDs: denial must precede
object lookup, preview replay and all domain commands.
"""
from copy import deepcopy
import time
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import F
from django.http import HttpResponse
from django.test import Client, RequestFactory, override_settings
from django.urls import include, path, reverse

from tseh.models import AccountAccess, AuditEntry, Dish, DomainState, Operation, RestoreSession
from tseh.services import backups
from tests.test_stage2_services import item, token


pytestmark = pytest.mark.django_db
MISSING = uuid.UUID('dd01449d-024b-4f09-b2e7-98002845a1bd')
SECTIONS = ('dishes', 'preparations', 'ingredients', 'prices', 'purchases', 'suppliers')
READ_PERMISSIONS = [f'{section}.view' for section in SECTIONS] + ['journal.view']
ALL_PERMISSIONS = [f'{section}.{action}' for section in SECTIONS
                   for action in ('view', 'add', 'change', 'delete')] + ['journal.view']

READ_ROUTES = [
    ('dashboard', ()), ('ingredients', ()), ('ingredient-detail', (MISSING,)),
    ('price-list', (MISSING,)), ('suppliers', ()), ('preparations', ()),
    ('preparation-detail', (MISSING,)), ('dish-detail', (MISSING,)),
    ('purchase-list', ()), ('purchase-detail', (MISSING,)), ('delivery-list', ()),
    ('journal', ()), ('archive', ()), ('scenario-editor', ()),
]
WRITE_ROUTES = [
    ('supplier-create', ()), ('supplier-edit', (MISSING,)),
    ('ingredient-create', ()), ('ingredient-edit', (MISSING,)),
    ('dish-create', ()), ('dish-edit', (MISSING,)),
    ('preparation-create', ()), ('preparation-edit', (MISSING,)),
    ('price-create', (MISSING,)), ('price-apply', (MISSING, MISSING)),
    ('price-correct', (MISSING, MISSING)), ('price-delete', (MISSING, MISSING)),
    ('purchase-create', ()), ('purchase-edit', (MISSING,)),
    ('purchase-line-delete', (MISSING, MISSING)),
    ('delivery-create', ()), ('delivery-edit', (MISSING,)),
    ('delivery-delete', (MISSING,)), ('delivery-attach', (MISSING,)),
    *[('dish-transition', (MISSING, action))
      for action in ('archive', 'trash', 'restore', 'activate')],
    *[('catalog-transition', (kind, MISSING, action))
      for kind in ('supplier', 'ingredient', 'preparation')
      for action in ('trash', 'restore', 'purge')],
]
OWNER_ROUTES = [
    ('thresholds', ()), ('exchange', ()), ('export-json', ()), ('export-xlsx', ()),
    ('restore-review', (MISSING,)), ('restore-download', (MISSING,)),
    ('restore-refresh', (MISSING,)),
]


@pytest.fixture
def employee_factory(user):
    """The legacy user fixture supplies a separate explicit owner."""
    def make(permissions=(), *, profile=True, active=True, superuser=False):
        person = get_user_model().objects.create_user(
            username='employee-' + uuid.uuid4().hex[:12],
            is_active=active, is_staff=superuser, is_superuser=superuser,
        )
        if profile:
            AccountAccess.objects.update_or_create(user=person, defaults={
                'is_owner': False, 'permissions': list(permissions),
            })
        else:
            AccountAccess.objects.filter(user=person).delete()
        client = Client()
        client.force_login(person)
        return person, client
    return make


def facts():
    state = DomainState.objects.get(pk=1)
    return {
        'epoch': state.epoch, 'revision': state.revision,
        'data': {name: list(model.objects.order_by('pk').values())
                 for name, model in backups.TABLES.items()},
        'operations': list(Operation.objects.order_by('pk').values()),
        'restore_sessions': list(RestoreSession.objects.order_by('pk').values()),
    }


def url(case):
    return reverse(case[0], args=case[1] or None)


def rejected(response):
    """Revocation may forbid a live session or invalidate it entirely."""
    assert response.status_code in (302, 403)
    if response.status_code == 302:
        assert response.url.startswith(reverse('login'))


def revoke(person):
    AccountAccess.objects.filter(user=person).update(
        permissions=READ_PERMISSIONS, version=F('version') + 1,
    )


def recipe_preview(client, component, route='dish-create'):
    target = reverse(route)
    initial = client.get(target)
    assert initial.status_code == 200
    body = {
        'token': initial.context['form'].initial['token'],
        'name': 'Рецептура сотрудника', 'base_price': '1000',
        'output_quantity': '1', 'output_unit': 'kg',
        'form-TOTAL_FORMS': '1', 'form-INITIAL_FORMS': '0',
        'form-0-component': f'ingredient:{component}',
        'form-0-quantity': '100', 'form-0-unit': 'g', 'intent': 'preview',
    }
    preview = client.post(target, body)
    assert preview.status_code == 200
    assert preview.context['preview_token']
    return target, {**body, 'intent': 'save', 'preview_token': preview.context['preview_token']}


@pytest.mark.parametrize('case', READ_ROUTES + WRITE_ROUTES + OWNER_ROUTES,
                         ids=lambda value: value[0] + ':' + ','.join(map(str, value[1])))
def test_employee_without_permissions_cannot_get_or_post_any_domain_route(employee_factory, state, case):
    _, client = employee_factory()
    before = facts()
    assert client.get(url(case), follow=True).status_code == 403
    assert client.post(url(case), {'intent': 'confirm', 'preview_token': 'forged'}).status_code == 403
    assert facts() == before


@pytest.mark.parametrize('case', WRITE_ROUTES,
                         ids=lambda value: value[0] + ':' + ','.join(map(str, value[1])))
def test_view_permissions_never_allow_mutation_or_editor_access(employee_factory, state, case):
    _, client = employee_factory(READ_PERMISSIONS)
    before = facts()
    assert client.get(url(case)).status_code == 403
    assert client.post(url(case), {'intent': 'confirm', 'preview_token': 'forged'}).status_code == 403
    assert facts() == before


@pytest.mark.parametrize('case', OWNER_ROUTES,
                         ids=lambda value: value[0])
def test_all_business_permissions_do_not_grant_owner_operations(employee_factory, state, case):
    _, client = employee_factory(ALL_PERMISSIONS)
    before = facts()
    assert client.get(url(case)).status_code == 403
    assert client.post(url(case), {'intent': 'confirm'}).status_code == 403
    assert facts() == before


@pytest.mark.parametrize('superuser', [False, True])
def test_missing_access_profile_is_denied_even_for_django_superuser(employee_factory, state, superuser):
    _, client = employee_factory(profile=False, superuser=superuser)
    before = facts()
    assert client.get(reverse('dashboard'), follow=True).status_code == 403
    assert client.post(reverse('supplier-create'), {'name': 'Не создавать'}).status_code == 403
    assert client.get(reverse('export-json')).status_code == 403
    assert facts() == before


@pytest.mark.parametrize('route', ['dashboard', 'ingredients', 'suppliers', 'preparations',
                                  'purchase-list', 'delivery-list', 'journal', 'archive'])
def test_read_permissions_allow_existing_list_pages(employee_factory, state, route):
    _, client = employee_factory(READ_PERMISSIONS)
    assert client.get(reverse(route)).status_code == 200


@pytest.mark.parametrize('route', ['dashboard', 'ingredients', 'suppliers', 'preparations',
                                  'purchase-list', 'journal', 'thresholds', 'exchange',
                                  'export-json', 'export-xlsx'])
def test_owner_can_read_existing_pages_and_downloads(authenticated, state, route):
    assert authenticated.get(reverse(route)).status_code == 200


def test_employee_landing_uses_first_allowed_section_without_login_loop(employee_factory, state):
    _, client = employee_factory(['suppliers.view'])
    response = client.get(reverse('dashboard'), follow=True)
    assert response.status_code == 200
    assert response.request['PATH_INFO'] == reverse('suppliers')
    assert all(not target.startswith(reverse('login')) for target, _ in response.redirect_chain)


def test_create_only_employee_can_save_but_cannot_edit_or_archive(employee_factory, state):
    person, client = employee_factory([*READ_PERMISSIONS, 'dishes.add'])
    component = item()
    target, body = recipe_preview(client, component)
    saved = client.post(target, body)
    assert saved.status_code == 302
    dish = Dish.objects.get()
    assert AuditEntry.objects.get(object_type='Dish', object_id=str(dish.pk)).actor_name == person.username
    before = facts()
    for case in [('dish-edit', (dish.pk,)), ('dish-transition', (dish.pk, 'archive')),
                 ('dish-transition', (dish.pk, 'trash'))]:
        assert client.get(url(case)).status_code == 403
        assert client.post(url(case), {'intent': 'save', 'token': token()}).status_code == 403
    assert facts() == before


@pytest.mark.parametrize('route', ['dish-create', 'preparation-create'])
def test_permission_revoked_after_recipe_preview_cannot_save(employee_factory, state, route):
    person, client = employee_factory(ALL_PERMISSIONS)
    target, body = recipe_preview(client, item(), route)
    revoke(person)
    before = facts()
    rejected(client.post(target, body))
    assert facts() == before


def test_permission_revoked_after_valid_price_preview_cannot_execute_early_confirmation(employee_factory, state):
    from tests.test_price_web import SuccessfulControls, preview
    person, client = employee_factory(ALL_PERMISSIONS)
    component = item()
    response, _ = preview(client, 'create', component, pack_size='1', pack_price='1200',
                          price_date='2026-11-09', apply_now='on')
    body = SuccessfulControls(response.content.decode()).values
    body['intent'] = 'confirm'
    revoke(person)
    before = facts()
    rejected(client.post(reverse('price-create', args=[component]), body))
    assert facts() == before


def test_price_add_permission_cannot_apply_price_through_create_checkbox(employee_factory, state):
    from tests.test_price_web import initial
    _, client = employee_factory([*READ_PERMISSIONS, 'prices.add'])
    component = item()
    body = initial(client, 'create', component, pack_size='1', pack_price='1200',
                   price_date='2026-11-09', apply_now='on')
    before = facts()
    assert client.post(reverse('price-create', args=[component]), body).status_code == 403
    assert facts() == before


def test_revoking_only_price_change_blocks_existing_add_and_apply_confirmation(employee_factory, state):
    from tests.test_price_web import SuccessfulControls, preview
    person, client = employee_factory(ALL_PERMISSIONS)
    component = item()
    response, _ = preview(client, 'create', component, pack_size='1', pack_price='1200',
                          price_date='2026-11-09', apply_now='on')
    body = {**SuccessfulControls(response.content.decode()).values, 'intent': 'confirm'}
    AccountAccess.objects.filter(user=person).update(
        permissions=[*READ_PERMISSIONS, 'prices.add'], version=F('version') + 1,
    )
    before = facts()
    rejected(client.post(reverse('price-create', args=[component]), body))
    assert facts() == before


def test_read_only_scenario_preview_cannot_be_applied(employee_factory, state):
    from tests.test_scenario_web import controls, initial, preview, row
    _, client = employee_factory(READ_PERMISSIONS)
    response = preview(client, initial(client, [row(item(), '1200')]))
    before = facts()
    assert client.post(reverse('scenario-editor'), {**controls(response), 'intent': 'confirm'}).status_code == 403
    assert facts() == before


def test_permission_revoked_after_valid_scenario_preview_cannot_apply_any_price(employee_factory, state):
    from tests.test_scenario_web import controls, initial, preview, row
    person, client = employee_factory(ALL_PERMISSIONS)
    response = preview(client, initial(client, [row(item(), '1200')]))
    body = {**controls(response), 'intent': 'confirm'}
    revoke(person)
    before = facts()
    rejected(client.post(reverse('scenario-editor'), body))
    assert facts() == before


@pytest.mark.parametrize('kind', ['purchase', 'delivery', 'line-delete'])
def test_permission_revoked_after_purchase_or_delivery_preview_cannot_execute(employee_factory, state, kind):
    from tseh.models import PurchaseLine
    from tests.test_purchase_web import controls, create_body, line_input, preview, purchase
    person, client = employee_factory(ALL_PERMISSIONS)
    if kind == 'purchase':
        route, args = 'purchase-create', ()
        response = preview(client, route, data=create_body(client, line_input(item())))
    elif kind == 'delivery':
        route, args = 'delivery-create', ()
        response = preview(client, route, amount='3000', note='Доставка')
    else:
        purchase_id = purchase(line_input(item()), line_input(item()), delivery='0')
        purchase_line = PurchaseLine.objects.filter(purchase_id=purchase_id).order_by('position').first()
        route, args = 'purchase-line-delete', (purchase_id, purchase_line.pk)
        response = preview(client, route, *args, delivery_action='drop_share')
    body = {**controls(response), 'intent': 'confirm'}
    revoke(person)
    before = facts()
    rejected(client.post(reverse(route, args=args or None), body))
    assert facts() == before


def test_purchase_permissions_include_only_required_price_operations():
    from tseh.access import normalize_permissions

    editing = set(normalize_permissions(['purchases.change']))
    deleting = set(normalize_permissions(['purchases.delete']))
    price_deleting = set(normalize_permissions(['prices.delete']))
    assert {'prices.add', 'prices.change'} <= editing
    assert 'prices.delete' not in editing
    assert {'prices.delete', 'prices.change'} <= deleting
    assert 'prices.add' not in deleting
    assert 'prices.add' not in price_deleting
    assert 'prices.change' not in price_deleting


def current_purchase_line():
    """A purchased price is current; the original offer can replace it."""
    from tseh.models import Ingredient, Price, PurchaseLine
    from tseh.services.prices import apply_price
    from tests.test_purchase_web import line_input, purchase

    raw = item()
    original = Ingredient.objects.get(pk=raw).current_price_id
    purchase_id = purchase(line_input(raw))
    linked = Price.objects.get(purchase_id=purchase_id)
    apply_price(token(), raw, linked.pk)
    row = PurchaseLine.objects.get(purchase_id=purchase_id)
    return raw, original, purchase_id, row.pk


NEW_PURCHASE_REPLACEMENT = {
    'delivery_action': 'keep', 'replacement_mode': 'new',
    'replacement-pack_size': '1', 'replacement-pack_price': '1200',
    'replacement-price_date': '2026-11-04',
}


def test_purchase_delete_without_price_add_can_select_existing_replacement_only(employee_factory, state):
    from tseh.access import normalize_permissions
    from tseh.models import Ingredient, PurchaseLine
    from tests.test_purchase_web import controls, confirm

    _, client = employee_factory(normalize_permissions(['purchases.delete']))
    raw, original, purchase_id, line_id = current_purchase_line()
    target = reverse('purchase-line-delete', args=[purchase_id, line_id])
    response = client.get(target)
    assert response.status_code == 200
    assert 'new' not in dict(response.context['form'].fields['replacement_mode'].choices)
    assert response.context['replacement_form'] is None
    before = facts()
    forged = {**controls(response), **NEW_PURCHASE_REPLACEMENT, 'intent': 'preview'}
    assert client.post(target, forged).status_code == 403
    assert facts() == before

    confirm(client, 'purchase-line-delete', purchase_id, line_id, delivery_action='keep',
            replacement_mode='existing', replacement_price=str(original))
    assert not PurchaseLine.objects.filter(pk=line_id).exists()
    assert Ingredient.objects.get(pk=raw).current_price_id == original


@pytest.mark.parametrize('already_saved', [False, True], ids=['live-preview', 'deleted-object-replay'])
def test_purchase_new_replacement_rechecks_add_right_before_confirm_or_replay(
        employee_factory, state, already_saved):
    from tseh.access import normalize_permissions
    from tests.test_purchase_web import controls, preview

    person, client = employee_factory(ALL_PERMISSIONS)
    _, _, purchase_id, line_id = current_purchase_line()
    target = reverse('purchase-line-delete', args=[purchase_id, line_id])
    response = preview(client, 'purchase-line-delete', purchase_id, line_id,
                       **NEW_PURCHASE_REPLACEMENT)
    confirmation = {**controls(response), 'intent': 'confirm'}
    if already_saved:
        assert client.post(target, confirmation).status_code == 302
    AccountAccess.objects.filter(user=person).update(
        permissions=normalize_permissions(['purchases.delete']), version=F('version') + 1,
    )
    before = facts()
    assert client.post(target, confirmation).status_code == 403
    assert facts() == before


def test_price_delete_does_not_imply_apply_or_create_replacement(employee_factory, authenticated, state):
    from tseh.access import normalize_permissions
    from tseh.models import Ingredient, Price
    from tests.test_price_web import confirm, offer

    raw = item()
    current = Ingredient.objects.get(pk=raw).current_price_id
    unused = offer(authenticated, raw)
    _, client = employee_factory(normalize_permissions(['prices.delete']))
    before = facts()
    target = reverse('price-delete', args=[raw, current])
    assert client.get(target).status_code == 403
    assert client.post(target, {'intent': 'preview', 'replacement_mode': 'new'}).status_code == 403
    assert facts() == before
    confirm(client, 'delete', raw, unused)
    assert not Price.objects.filter(pk=unused).exists()
    assert Ingredient.objects.get(pk=raw).current_price_id == current


@pytest.mark.parametrize('kind', ['supplier', 'ingredient'])
def test_catalog_dependency_names_stay_private_on_get_and_forged_post(employee_factory, state, kind):
    from decimal import Decimal
    from tseh.access import normalize_permissions
    from tseh.models import Ingredient
    from tseh.services.commands import save_dish
    from tests.test_stage2_services import line

    raw = item()
    secret = 'SECRET-dependent-' + uuid.uuid4().hex
    if kind == 'supplier':
        Ingredient.objects.filter(pk=raw).update(name=secret)
        key = Ingredient.objects.select_related('current_price').get(pk=raw).current_price.supplier_id
    else:
        key = raw
        save_dish(token(), {'name': secret, 'base_price': Decimal('1000')}, [line(raw)])
    _, client = employee_factory(normalize_permissions([kind + 's.delete']))
    target = reverse('catalog-transition', args=[kind, key, 'trash'])
    response = client.get(target)
    assert response.status_code == 200
    assert secret not in response.content.decode()
    assert 'Есть связанные записи в недоступных вам разделах' in response.content.decode()
    before = facts()
    response = client.post(target, {'token': response.context['form'].initial['token']})
    assert response.status_code == 400
    assert secret not in response.content.decode()
    assert secret not in str(response.context['form'].errors)
    assert 'Есть связанные записи в недоступных вам разделах' in response.content.decode()
    assert facts() == before


def test_nested_and_combined_dependency_messages_hide_entire_private_reference(employee_factory):
    from tseh.access import Access, visible_dependencies

    person, _ = employee_factory(['dishes.view'])
    visible = 'Блюдо: Доступное название'
    messages = [visible, 'Ошибка: Цены позиции: SECRET-priced',
                'Блюдо: Доступное название; Заготовка: SECRET-preparation\nSECRET-continuation',
                'Ошибка: Цена позиции: SECRET-single']
    result = visible_dependencies(Access(person), messages)
    assert result[0] == visible
    assert len(result) == 2
    assert 'Есть связанные записи в недоступных вам разделах' in result[1]
    assert 'SECRET-' not in '\n'.join(result)


@pytest.mark.parametrize('route', ['dish-create', 'preparation-create'])
def test_recipe_preview_cannot_be_reused_by_another_authorized_employee(employee_factory, state, route):
    _, first = employee_factory(ALL_PERMISSIONS)
    _, second = employee_factory(ALL_PERMISSIONS)
    target, body = recipe_preview(first, item(), route)
    before = facts()
    response = second.post(target, body)
    assert response.status_code in (200, 400, 403)
    assert facts() == before
    if response.status_code == 200:
        assert response.context['preview_token'] != body['preview_token']


@pytest.mark.parametrize('route', ['dish-create', 'preparation-create'])
def test_recipe_preview_expires_after_one_hour(employee_factory, state, route, monkeypatch):
    _, client = employee_factory(ALL_PERMISSIONS)
    target, body = recipe_preview(client, item(), route)
    before = facts()
    future = time.time() + 3601
    monkeypatch.setattr('django.core.signing.time.time', lambda: future)
    response = client.post(target, body)
    assert response.status_code in (200, 400, 403)
    assert facts() == before


def test_disabled_employee_existing_session_loses_access(employee_factory, state):
    person, client = employee_factory(ALL_PERMISSIONS)
    assert client.get(reverse('dashboard')).status_code == 200
    get_user_model().objects.filter(pk=person.pk).update(is_active=False)
    before = facts()
    rejected(client.get(reverse('dashboard')))
    rejected(client.post(reverse('supplier-create'), {'token': token(), 'name': 'Не создавать', 'note': ''}))
    assert facts() == before


@pytest.mark.parametrize('revocation', ['session-deleted', 'password-reset'])
def test_post_revalidates_already_loaded_authentication_after_waiting_for_access_lock(
        employee_factory, state, revocation):
    """Simulate AuthenticationMiddleware finishing before an owner revokes access."""
    from tseh.middleware import AccountAccessMiddleware

    person, client = employee_factory(ALL_PERMISSIONS)
    request = RequestFactory().post(reverse('supplier-create'), {'name': 'Не создавать'})
    request.user = person
    request.session = client.session
    # Materialize the valid old session before the concurrent administrative change.
    assert request.session['_auth_user_id'] == str(person.pk)
    old_key = request.session.session_key
    if revocation == 'session-deleted':
        Session.objects.filter(session_key=old_key).delete()
    else:
        fresh = get_user_model().objects.get(pk=person.pk)
        fresh.set_password('replacement-password-for-race-test')
        fresh.save(update_fields=['password'])
        assert Session.objects.filter(session_key=old_key).exists()

    called = []
    def handler(_request):
        called.append(True)
        return HttpResponse('The write handler must not run')

    before = facts()
    with pytest.raises(PermissionDenied):
        AccountAccessMiddleware(handler)(request)
    assert called == []
    assert facts() == before


def test_employee_cannot_upload_valid_backup_or_use_existing_restore_session(employee_factory, user, state):
    _, client = employee_factory(ALL_PERMISSIONS)
    document = backups.export_snapshot()
    prepared = backups.prepare_restore(user, document)
    before = facts()
    upload = SimpleUploadedFile('copy.json', backups.encode_backup(document), 'application/json')
    assert client.post(reverse('exchange'), {'backup_file': upload}).status_code == 403
    for route in ('restore-review', 'restore-download', 'restore-refresh'):
        target = reverse(route, args=[prepared.pk])
        assert client.get(target).status_code == 403
        assert client.post(target, {'confirm_download': 'on',
                                   'review_token': backups.restore_token(prepared, user)}).status_code == 403
    assert facts() == before


def test_domain_restore_preserves_account_access_and_accepts_legacy_actorless_journal(employee_factory, user, state):
    employee_factory(['suppliers.view'])
    item()
    document = backups.export_snapshot()
    assert 'account_access' not in document['data']
    legacy = deepcopy(document)
    for entry in legacy['data']['journal']:
        entry.pop('actor_name', None)
    backups.validate_backup(legacy)
    profiles = list(AccountAccess.objects.order_by('pk').values())
    accounts = list(get_user_model().objects.order_by('pk').values())
    prepared = backups.prepare_restore(user, legacy)
    backups.download_backup(prepared.pk, user)
    backups.apply_restore(prepared.pk, user, review_token=backups.restore_token(prepared, user),
                          confirm_download=True)
    assert list(AccountAccess.objects.order_by('pk').values()) == profiles
    assert list(get_user_model().objects.order_by('pk').values()) == accounts
    assert not AuditEntry.objects.exclude(action='restore').exclude(actor_name='').exists()
    backups.validate_backup(backups.export_snapshot())


def _unmapped_view(request):
    if request.method == 'POST':
        DomainState.objects.filter(pk=1).update(revision=F('revision') + 1)
    return HttpResponse('Private unmapped route')


urlpatterns = [path('__unmapped__/', _unmapped_view, name='unmapped-private-route'),
               path('', include('config.urls'))]


def test_new_unmapped_application_route_is_denied_by_default(employee_factory, state):
    _, client = employee_factory(ALL_PERMISSIONS)
    before = facts()
    with override_settings(ROOT_URLCONF=__name__):
        assert client.get('/__unmapped__/').status_code == 403
        assert client.post('/__unmapped__/').status_code == 403
    assert facts() == before
