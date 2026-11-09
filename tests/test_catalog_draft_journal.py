"""The audit page must display incomplete draft values without parsing them as costs."""
import pytest
from django.urls import reverse
from tseh.models import Dish, Ingredient, Preparation
from tseh.services.catalog_drafts import draft_token, save_draft

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('kind,payload', [
    ('ingredient', {'pack_price': '', 'loss': 'уточнить'}),
    ('preparation', {'output_quantity': '110–120', 'lines': [
        {'label': 'Учебный компонент', 'quantity': '10–20', 'unit': '', 'note': 'Проверить единицу'}]}),
    ('dish', {'base_price': '', 'lines': [{'label': 'Учебный компонент', 'quantity': 'по вкусу'}]}),
])
def test_journal_filters_and_escapes_incomplete_draft_source(authenticated, user, state, kind, payload):
    save_draft(user, token=draft_token(user, kind), kind=kind, name='Неполная учебная карточка',
               payload=payload, source_text='<script>source-only</script>', notes='Заполнить вручную')
    response = authenticated.get(reverse('journal'), {'kind': 'CatalogDraft'})
    assert response.status_code == 200
    content = response.content.decode()
    assert 'Черновик создан' in content and 'Неполная учебная карточка' in content
    assert 'Исходный текст' in content and 'Заполнить вручную' in content
    assert '&lt;script&gt;source-only&lt;/script&gt;' in content
    assert '<script>source-only</script>' not in content
    assert not Ingredient.objects.exists() and not Preparation.objects.exists() and not Dish.objects.exists()
    if kind == 'ingredient':
        assert 'Не указано' in content and 'уточнить' in content
    elif kind == 'preparation':
        assert '110–120' in content and '10–20' in content and 'единица не указана' in content
    else:
        assert 'по вкусу' in content
