"""Independent draft security checks using synthetic data only.

Draft persistence must never silently create usable stock/prices/recipes. Tests
exercise services and HTTP boundaries, including confirmations issued before
permissions, drafts or the domain state change.
"""
from copy import deepcopy
from decimal import Decimal
import time
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import F
from django.test import Client
from django.urls import reverse

from tseh.access import normalize_permissions
from tseh.models import (AccountAccess, AuditEntry, CatalogDraft, Dish, DomainState, Ingredient,
                         Operation, Preparation, Price)
from tseh.services import backups, catalog_drafts
from tseh.services.lifecycle import transition_catalog
from tseh.services.queries import dish_calculation
from tests.test_stage2_services import item, token


pytestmark = pytest.mark.django_db
SECTION = {'ingredient': 'ingredients', 'preparation': 'preparations', 'dish': 'dishes'}
SOURCE = 'Синтетическая карточка для теста: 30–40 г; количество уточнить.'


@pytest.fixture
def employee(user):
    def create(permissions=()):
        person = get_user_model().objects.create_user(username='draft-staff-' + uuid.uuid4().hex[:9])
        AccountAccess.objects.create(user=person, permissions=normalize_permissions(permissions))
        client = Client()
        client.force_login(person)
        return person, client
    return create


def payload(kind, **changes):
    values = {
        'ingredient': {'unit': 'kg', 'ingredient_kind': 'ingredient', 'loss': '0', 'supplier': '',
                       'price_date': '2026-11-04', 'pack_size': '1', 'pack_price': '',
                       'zero_price_confirmed': False},
        'preparation': {'output_quantity': '1', 'output_unit': 'kg', 'lines': []},
        'dish': {'base_price': '1000', 'green_override': '', 'orange_override': '', 'lines': []},
    }[kind]
    return {**values, **changes}


def draft_line(reference='', quantity='100', unit='g', **changes):
    return {'component': reference, 'label': 'Синтетический компонент',
            'quantity': quantity, 'unit': unit, 'note': '', **changes}


def make(actor, kind='ingredient', *, data=None, name=None, source_text=SOURCE, **options):
    return catalog_drafts.save_draft(actor, token=catalog_drafts.draft_token(actor, kind),
        kind=kind, name=name or 'Тестовый черновик ' + uuid.uuid4().hex[:8],
        payload=data if data is not None else payload(kind), source_text=source_text, notes='', **options)


def edit(actor, draft, *, command_token=None, **changes):
    return catalog_drafts.save_draft(actor,
        token=command_token or catalog_drafts.draft_token(actor, draft.kind, draft),
        draft_id=draft.pk, kind=draft.kind, name=changes.pop('name', draft.name),
        payload=changes.pop('payload', draft.payload), source_text=changes.pop('source_text', draft.source_text),
        notes=changes.pop('notes', draft.notes), **changes)


def preview(actor, draft):
    result = catalog_drafts.preview_draft(actor, draft.pk)
    assert result['valid'], result.get('issues')
    assert result['preview_token']
    return result['preview_token']


def facts():
    state = DomainState.objects.get(pk=1)
    return {'epoch': state.epoch, 'revision': state.revision,
            'tables': {name: list(model.objects.order_by('pk').values()) for name, model in backups.TABLES.items()},
            'drafts': list(CatalogDraft.objects.order_by('pk').values()),
            'operations': list(Operation.objects.order_by('pk').values())}


def working_facts():
    return {name: list(model.objects.order_by('pk').values()) for name, model in backups.TABLES.items()
            if name not in {'catalog_drafts', 'journal'}}


@pytest.mark.parametrize('kind', list(SECTION))
def test_incomplete_draft_preserves_source_and_does_not_enter_working_catalog(user, authenticated, state, kind):
    before = working_facts()
    data = payload(kind)
    if kind != 'ingredient':
        data['lines'] = [draft_line(quantity='30–40', note='Уточнить количество')]
    draft = make(user, kind, data=data)
    draft.refresh_from_db()
    assert draft.source_text == SOURCE
    assert draft.payload == data
    assert not draft.is_published and draft.published_object_id is None
    assert not catalog_drafts.preview_draft(user, draft.pk)['valid']
    assert working_facts() == before
    response = authenticated.get(reverse('draft-detail', args=[draft.pk]))
    assert response.status_code == 200 and draft.name in response.content.decode()
    # Drafts must be absent from ordinary catalog lists even after a reload.
    route = {'ingredient': 'ingredients', 'preparation': 'preparations', 'dish': 'dashboard'}[kind]
    assert draft.name not in authenticated.get(reverse(route)).content.decode()


@pytest.mark.parametrize('price,confirmed,valid', [('', True, False), ('0', False, False), ('0', True, True)])
def test_unknown_price_is_not_a_free_price_and_real_zero_requires_confirmation(user, state, price, confirmed, valid):
    draft = make(user, data=payload('ingredient', pack_price=price, zero_price_confirmed=confirmed))
    before = working_facts()
    result = catalog_drafts.preview_draft(user, draft.pk)
    assert result['valid'] is valid
    assert working_facts() == before
    if valid:
        key = catalog_drafts.publish_draft(user, draft.pk, result['preview_token'])
        assert Ingredient.objects.get(pk=key).current_price.pack_price == Decimal('0')
    else:
        assert not Ingredient.objects.exists() and not Price.objects.exists()


@pytest.mark.parametrize('kind', list(SECTION))
def test_kind_read_permission_cannot_create_edit_publish_or_delete_drafts(user, employee, state, kind):
    draft = make(user, kind)
    _, browser = employee([SECTION[kind] + '.view'])
    before = facts()
    assert browser.get(reverse('draft-detail', args=[draft.pk])).status_code == 200
    targets = [reverse('draft-create', args=[kind]),
               *[reverse(route, args=[draft.pk]) for route in ('draft-edit', 'draft-preview', 'draft-delete')]]
    for target in targets:
        assert browser.get(target).status_code in (403, 404)
        assert browser.post(target, {'token': 'forged', 'preview_token': 'forged', 'confirm': 'on'}).status_code in (403, 404)
    assert facts() == before


def test_kind_is_taken_from_record_and_other_draft_names_are_not_disclosed(user, employee, state):
    secret = make(user, 'dish', name='SECRET-dish-draft')
    visible = make(user, 'ingredient', name='Visible raw draft')
    person, browser = employee(['ingredients.change'])
    before = facts()
    response = browser.get(reverse('draft-list'), {'kind': 'dish', 'state': 'all'})
    assert response.status_code == 200 and secret.name not in response.content.decode()
    assert browser.get(reverse('draft-detail', args=[secret.pk])).status_code in (403, 404)
    assert browser.post(reverse('draft-edit', args=[secret.pk]),
        {'kind': 'ingredient', 'name': 'Forged kind', 'token': catalog_drafts.draft_token(person, 'ingredient', visible)}).status_code in (403, 404)
    with pytest.raises((PermissionDenied, ValidationError)):
        catalog_drafts.save_draft(person, draft_id=secret.pk, kind='ingredient', name='Forged kind',
            payload=payload('ingredient'), token=catalog_drafts.draft_token(person, 'ingredient', visible))
    assert facts() == before


def test_add_only_employee_can_save_new_draft_and_publish_without_edit_permission(employee, state):
    person, browser = employee(['ingredients.add'])
    response = browser.get(reverse('draft-create', args=['ingredient']))
    assert response.status_code == 200
    body = {'token': response.context['form'].initial['token'], 'name': 'Add-only synthetic raw',
            'notes': '', 'source_text': SOURCE, **payload('ingredient', pack_price='2000')}
    body.pop('zero_price_confirmed')
    assert browser.post(reverse('draft-create', args=['ingredient']), body).status_code == 302
    draft = CatalogDraft.objects.get(name=body['name'])
    assert browser.get(reverse('draft-edit', args=[draft.pk])).status_code in (403, 404)
    response = browser.get(reverse('draft-preview', args=[draft.pk]))
    assert response.status_code == 200 and response.context['preview']['valid']
    assert browser.post(reverse('draft-preview', args=[draft.pk]),
        {'preview_token': response.context['preview']['preview_token'], 'confirm': 'on'}).status_code == 302
    draft.refresh_from_db()
    assert draft.is_published and Ingredient.objects.filter(pk=draft.published_object_id).exists()
    assert AuditEntry.objects.filter(actor_name=person.username, object_type='CatalogDraft').exists()


def test_http_saves_incomplete_recipe_and_shows_publication_issues_without_creating_dish(user, authenticated, state):
    target = reverse('draft-create', args=['dish'])
    response = authenticated.get(target)
    body = {'token': response.context['form'].initial['token'], 'name': 'Synthetic incomplete recipe',
            'notes': 'Количество ещё не уточнено', 'source_text': SOURCE,
            'base_price': '', 'green_override': '', 'orange_override': '',
            'lines-TOTAL_FORMS': '1', 'lines-INITIAL_FORMS': '0',
            'lines-0-component': '', 'lines-0-label': 'Компонент из исходника',
            'lines-0-quantity': '30–40', 'lines-0-unit': 'g', 'lines-0-note': 'Сохранить диапазон'}
    assert authenticated.post(target, body).status_code == 302
    draft = CatalogDraft.objects.get(name=body['name'])
    assert draft.payload['lines'][0]['quantity'] == '30–40'
    before = facts()
    preview_url = reverse('draft-preview', args=[draft.pk])
    response = authenticated.get(preview_url)
    assert response.status_code == 200
    assert not response.context['preview']['valid'] and response.context['preview']['issues']
    assert '30–40' in response.content.decode()
    response = authenticated.post(preview_url, {'preview_token': 'forged', 'confirm': 'on'})
    assert response.status_code == 400 and response.context['publication_errors']
    assert facts() == before and not Dish.objects.exists()


def test_http_raw_draft_can_save_only_name_without_implying_kind_units_loss_or_price(authenticated, state):
    target = reverse('draft-create', args=['ingredient'])
    response = authenticated.get(target)
    before = working_facts()
    assert authenticated.post(target, {'token': response.context['form'].initial['token'],
                                      'name': 'Only a synthetic name'}).status_code == 302
    draft = CatalogDraft.objects.get(name='Only a synthetic name')
    for key in ('ingredient_kind', 'unit', 'loss', 'pack_size', 'pack_price', 'price_date'):
        assert draft.payload[key] == ''
    assert draft.payload['zero_price_confirmed'] is False
    response = authenticated.get(reverse('draft-preview', args=[draft.pk]))
    assert response.status_code == 200
    assert not response.context['preview']['valid']
    assert response.context['preview']['issues']
    assert working_facts() == before


def test_http_imported_draft_edit_preserves_readonly_source_and_uses_real_actor(user, employee, state):
    draft = make(user, source='import')
    person, browser = employee(['ingredients.change'])
    target = reverse('draft-edit', args=[draft.pk])
    response = browser.get(target)
    assert response.status_code == 200
    assert 'source_text' not in response.context['form'].fields
    body = {'token': response.context['form'].initial['token'], 'name': draft.name, 'notes': 'Уточнил сотрудник',
            'source_text': 'FORGED original source', 'source': 'import', 'actor_name': 'forged-owner', **draft.payload}
    body.pop('zero_price_confirmed')
    assert browser.post(target, body).status_code == 302
    draft.refresh_from_db()
    assert draft.source_text == SOURCE and draft.source == 'import'
    assert draft.notes == 'Уточнил сотрудник'
    entry = AuditEntry.objects.filter(object_type='CatalogDraft', object_id=str(draft.pk)).latest('id')
    assert entry.actor_name == person.username and entry.source == 'manual'


def test_publish_confirmation_is_bound_to_actor_and_current_permissions(user, employee, state):
    first, first_browser = employee(['ingredients.add'])
    second, second_browser = employee(['ingredients.add'])
    draft = make(first, data=payload('ingredient', pack_price='2000'))
    signed = preview(first, draft)
    target = reverse('draft-preview', args=[draft.pk])
    before = facts()
    response = second_browser.post(target, {'preview_token': signed, 'confirm': 'on'})
    assert response.status_code in (400, 403)
    assert facts() == before
    AccountAccess.objects.filter(user=first).update(permissions=['ingredients.view'], version=F('version') + 1)
    assert first_browser.post(target, {'preview_token': signed, 'confirm': 'on'}).status_code in (403, 404)
    assert facts() == before


def test_conflicting_saves_and_old_preview_cannot_overwrite_latest_draft(user, state):
    draft = make(user, data=payload('ingredient', pack_price='2000'))
    first = catalog_drafts.draft_token(user, draft.kind, draft)
    second = catalog_drafts.draft_token(user, draft.kind, draft)
    signed = preview(user, draft)
    changed = edit(user, draft, command_token=first, notes='Сохранённая новая заметка')
    before = facts()
    with pytest.raises(ValidationError):
        edit(user, draft, command_token=second, notes='Потеря последней правки')
    with pytest.raises(ValidationError):
        catalog_drafts.publish_draft(user, draft.pk, signed)
    assert facts() == before
    assert CatalogDraft.objects.get(pk=changed.pk).notes == 'Сохранённая новая заметка'


def test_expired_publication_confirmation_has_no_effect(user, state, monkeypatch):
    draft = make(user, data=payload('ingredient', pack_price='2000'))
    signed = preview(user, draft)
    before = facts()
    future = time.time() + 2 * 86400
    monkeypatch.setattr('django.core.signing.time.time', lambda: future)
    with pytest.raises(ValidationError):
        catalog_drafts.publish_draft(user, draft.pk, signed)
    assert facts() == before


def test_publication_requires_explicit_confirmation_and_csrf_and_get_is_read_only(user, state):
    draft = make(user, data=payload('ingredient', pack_price='2000'))
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(user)
    target = reverse('draft-preview', args=[draft.pk])
    before = facts()
    response = browser.get(target)
    assert response.status_code == 200 and response.context['preview']['valid']
    assert facts() == before
    body = {'preview_token': response.context['preview']['preview_token']}
    assert browser.post(target, {**body, 'confirm': 'on'}).status_code == 403
    assert browser.post(target, {**body, 'csrfmiddlewaretoken': browser.cookies['csrftoken'].value}).status_code == 400
    assert facts() == before


def test_publication_replay_creates_one_object_even_after_other_changes(user, state):
    draft = make(user, data=payload('ingredient', pack_price='2000'))
    signed = preview(user, draft)
    key = catalog_drafts.publish_draft(user, draft.pk, signed)
    make(user, 'dish')
    before = facts()
    assert catalog_drafts.publish_draft(user, draft.pk, signed) == key
    assert facts() == before
    assert Ingredient.objects.count() == 1 and Price.objects.count() == 1
    receipt = CatalogDraft.objects.get(pk=draft.pk)
    assert receipt.published_object_id == key and receipt.source_text == SOURCE


def test_published_original_cannot_be_edited_or_deleted_as_unpublished_draft(user, state):
    draft = make(user, data=payload('ingredient', pack_price='2000'))
    key = catalog_drafts.publish_draft(user, draft.pk, preview(user, draft))
    draft.refresh_from_db()
    before = facts()
    with pytest.raises((ValidationError, PermissionDenied)):
        edit(user, draft, notes='Overwrite published receipt')
    with pytest.raises((ValidationError, PermissionDenied)):
        catalog_drafts.delete_draft(user, draft.pk,
            catalog_drafts.draft_token(user, draft.kind, draft, action='delete'))
    assert facts() == before
    assert Ingredient.objects.filter(pk=key).exists()


def test_publication_rolls_back_target_and_receipt_when_domain_audit_fails(user, state, monkeypatch):
    draft = make(user, data=payload('ingredient', pack_price='2000'))
    signed = preview(user, draft)
    before = facts()
    def fail(*_args, **_kwargs):
        raise RuntimeError('Synthetic audit failure')
    monkeypatch.setattr('tseh.services.commands.audit', fail)
    with pytest.raises(RuntimeError, match='Synthetic audit failure'):
        catalog_drafts.publish_draft(user, draft.pk, signed)
    assert facts() == before


def test_unresolved_draft_reference_blocks_publication_but_keeps_original_line(user, state):
    missing = uuid.uuid4()
    row = draft_line('draft:' + str(missing), quantity='30–40')
    draft = make(user, 'preparation', data=payload('preparation', lines=[row]))
    assert CatalogDraft.objects.get(pk=draft.pk).payload['lines'] == [row]
    assert not catalog_drafts.preview_draft(user, draft.pk)['valid']
    assert not Preparation.objects.exists()


def test_references_resolve_only_after_publication_and_cost_has_independent_expected_value(user, state):
    raw = make(user, data=payload('ingredient', pack_price='2000'))
    prep = make(user, 'preparation', data=payload('preparation',
        lines=[draft_line('draft:' + str(raw.pk), quantity='1000')]))
    dish = make(user, 'dish', data=payload('dish',
        lines=[draft_line('draft:' + str(prep.pk), quantity='100')]))
    assert not catalog_drafts.preview_draft(user, prep.pk)['valid']
    assert not catalog_drafts.preview_draft(user, dish.pk)['valid']
    catalog_drafts.publish_draft(user, raw.pk, preview(user, raw))
    catalog_drafts.publish_draft(user, prep.pk, preview(user, prep))
    dish_id = catalog_drafts.publish_draft(user, dish.pk, preview(user, dish))
    # 2000 tenge/kg × 1 kg / 1 kg output × 0.1 kg serving = 200 tenge.
    assert dish_calculation(Dish.objects.get(pk=dish_id))[0].total == Decimal('200.00')


def test_component_moved_to_trash_after_preview_cannot_be_published(user, state):
    raw = item()
    draft = make(user, 'dish', data=payload('dish', lines=[draft_line('ingredient:' + str(raw))]))
    signed = preview(user, draft)
    transition_catalog(token(), 'ingredient', raw, 'trash')
    before = facts()
    assert not catalog_drafts.preview_draft(user, draft.pk)['valid']
    with pytest.raises(ValidationError):
        catalog_drafts.publish_draft(user, draft.pk, signed)
    assert facts() == before


def test_self_reference_and_closing_draft_cycle_are_rejected_without_losing_saved_source(user, state):
    first = make(user, 'preparation')
    second = make(user, 'preparation', data=payload('preparation', lines=[draft_line('draft:' + str(first.pk))]))
    for linked in (first, second):
        before = facts()
        with pytest.raises(ValidationError):
            edit(user, first, payload=payload('preparation', lines=[draft_line('draft:' + str(linked.pk))]))
        assert facts() == before
    assert CatalogDraft.objects.get(pk=first.pk).source_text == SOURCE


@pytest.mark.parametrize('bad', [[], {'pack_price': 1.25}, {'pack_price': True}, {'pack_price': float('nan')},
    {'pack_price': {'nested': '1'}}, {'pack_price': '1' * 121}, {'pack_price': '\ud800'},
    {'published_object_id': str(uuid.uuid4())}, {'actor_name': 'forged-owner'}, {'unit': 'x' * 33},
    {'zero_price_confirmed': 'true'}])
def test_payload_types_lengths_unicode_and_server_keys_are_rejected(user, state, bad):
    before = facts()
    with pytest.raises(ValidationError):
        make(user, data=bad)
    assert facts() == before


@pytest.mark.parametrize('changes', [
    {'source_text': 'x' * 20001}, {'notes': 'x' * 8001}, {'name': 'x' * 161},
    {'payload': payload('dish', lines=[draft_line()] * 101)},
    {'payload': payload('dish', lines=[draft_line(note='x' * 1025)])},
    {'payload': payload('dish', lines=[draft_line(label='x' * 501)])},
])
def test_draft_size_limits_are_enforced_without_partial_writes(user, state, changes):
    before = facts()
    data = dict(changes)
    with pytest.raises(ValidationError):
        catalog_drafts.save_draft(user, token=catalog_drafts.draft_token(user, 'dish'), kind='dish',
            name=data.pop('name', 'Synthetic oversized draft'), payload=data.pop('payload', payload('dish')),
            source_text=data.pop('source_text', SOURCE), notes=data.pop('notes', ''))
    assert facts() == before


def test_source_is_escaped_immutable_and_import_actor_cannot_be_spoofed(user, authenticated, employee, state):
    attack = '</textarea><script>window.syntheticDraftAttack=1</script>'
    draft = make(user, 'dish', source_text=attack, source='import')
    creation = AuditEntry.objects.filter(object_type='CatalogDraft', object_id=str(draft.pk)).latest('id')
    assert creation.source == 'import' and creation.actor_name == user.username
    response = authenticated.get(reverse('draft-detail', args=[draft.pk]))
    assert response.status_code == 200 and attack not in response.content.decode()
    assert '&lt;script&gt;' in response.content.decode()
    before = facts()
    with pytest.raises(ValidationError):
        edit(user, draft, source_text='Changed provenance')
    assert facts() == before
    person, _ = employee(['dishes.change', 'dishes.add'])
    changed = edit(person, draft, notes='Уточнено сотрудником')
    assert changed.source == 'import' and changed.source_text == attack
    latest = AuditEntry.objects.filter(object_type='CatalogDraft', object_id=str(draft.pk)).latest('id')
    assert latest.source == 'manual' and latest.actor_name == person.username
    before = facts()
    with pytest.raises(PermissionDenied):
        make(person, 'dish', source='import')
    assert facts() == before


def test_backup_roundtrip_preserves_unfinished_sources_and_published_receipts_and_invalidates_forms(user, state):
    from tests.test_backups import apply
    raw = make(user, data=payload('ingredient', pack_price='2000'), source='import')
    raw_id = catalog_drafts.publish_draft(user, raw.pk, preview(user, raw))
    incomplete = make(user, 'dish', data=payload('dish', lines=[draft_line(quantity='30–40')]))
    old_form = catalog_drafts.draft_token(user, incomplete.kind, incomplete)
    document = backups.export_snapshot()
    assert document['schema_version'] == 2 and 'catalog_drafts' in document['data']
    expected = deepcopy(document['data']['catalog_drafts'])
    edit(user, incomplete, notes='Позднее изменение')
    apply(user, document)
    restored = backups.export_snapshot()
    assert restored['data']['catalog_drafts'] == expected
    assert CatalogDraft.objects.get(pk=raw.pk).published_object_id == raw_id
    assert CatalogDraft.objects.get(pk=incomplete.pk).payload['lines'][0]['quantity'] == '30–40'
    before = facts()
    with pytest.raises(ValidationError):
        edit(user, incomplete, command_token=old_form, notes='Старая форма после восстановления')
    assert facts() == before
    backups.validate_backup(restored)


def test_legacy_v1_backup_is_normalized_without_inventing_drafts(user, state):
    document = backups.export_snapshot()
    document['schema_version'] = 1
    document['data'].pop('catalog_drafts')
    validated = backups.validate_backup(document)
    assert validated['schema_version'] == 2
    assert validated['data']['catalog_drafts'] == []
