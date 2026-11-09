"""Explicit account permissions shared by server checks and presentation."""
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.urls import reverse

ACTION_LABELS = {"view": "Просматривать", "add": "Добавлять", "change": "Изменять", "delete": "Удалять"}
PERMISSION_GROUPS = (
    {"key": "dishes", "label": "Блюда", "description": "Удаление включает архив и корзину. Изменение — возврат в ассортимент.",
     "permissions": tuple(f"dishes.{a}" for a in ACTION_LABELS)},
    {"key": "preparations", "label": "Заготовки", "description": "Редактирование рецептур также открывает справочник сырья.",
     "permissions": tuple(f"preparations.{a}" for a in ACTION_LABELS)},
    {"key": "ingredients", "label": "Сырьё и упаковка", "description": "Добавление сырья включает его первую действующую цену.",
     "permissions": tuple(f"ingredients.{a}" for a in ACTION_LABELS)},
    {"key": "prices", "label": "Цены", "description": "Изменение включает применение и исправление цен. Работа с ценами открывает просмотр калькуляций и поставщиков; сценарий требует добавления и изменения цен.",
     "permissions": tuple(f"prices.{a}" for a in ACTION_LABELS)},
    {"key": "purchases", "label": "Закупки и доставка", "description": "Добавление закупок создаёт связанные цены. Изменение включает создание цен добавленных строк и пересчёт существующих. Удаление включает удаление и пересчёт связанных цен; новая цена для замены требует отдельного права добавления цен.",
     "permissions": tuple(f"purchases.{a}" for a in ACTION_LABELS)},
    {"key": "suppliers", "label": "Поставщики", "description": "Справочник поставщиков и их контакты.",
     "permissions": tuple(f"suppliers.{a}" for a in ACTION_LABELS)},
    {"key": "journal", "label": "Журнал", "description": "Полная история изменений всех разделов, включая названия и значения.",
     "permissions": ("journal.view",)},
)
PERMISSIONS = {key: ACTION_LABELS[key.split('.')[1]] for group in PERMISSION_GROUPS for key in group['permissions']}
DEPENDENCIES = {key: {key.split('.')[0] + '.view'} for key in PERMISSIONS if not key.endswith('.view')}
for key in ('dishes.add', 'dishes.change'):
    DEPENDENCIES[key] |= {'preparations.view', 'ingredients.view'}
for key in ('preparations.add', 'preparations.change'):
    DEPENDENCIES[key] |= {'ingredients.view'}
DEPENDENCIES['prices.view'] = {'ingredients.view'}
for action in ('add', 'change', 'delete'):
    DEPENDENCIES[f'prices.{action}'] |= {'dishes.view', 'preparations.view', 'ingredients.view', 'suppliers.view'}
    DEPENDENCIES[f'purchases.{action}'] |= {f'prices.{action}', 'ingredients.view', 'suppliers.view'}
DEPENDENCIES['purchases.view'] = {'ingredients.view', 'prices.view', 'suppliers.view'}
DEPENDENCIES['purchases.change'] |= {'prices.add'}
DEPENDENCIES['purchases.delete'] |= {'prices.change'}
PERMISSION_GROUPS[3]['description'] += ' Удаление действующей цены также требует изменения; создание замены — добавления.'


def normalize_permissions(values):
    if not isinstance(values, (list, tuple, set)) or any(not isinstance(v, str) or v not in PERMISSIONS for v in values):
        raise ValidationError('Неизвестное разрешение. Обновите форму сотрудника.')
    result = set(values)
    while True:
        expanded = result | {item for key in result for item in DEPENDENCIES.get(key, ())}
        if expanded == result:
            return sorted(result)
        result = expanded


class Access:
    def __init__(self, user, profile=None):
        self.user = user
        self.is_owner = False
        self.is_admin = False
        self.role = None
        self.role_label = "Без доступа"
        self.permissions = frozenset()
        if not user.is_authenticated or not user.is_active:
            return
        if profile is None:
            from tseh.models import AccountAccess
            profile = AccountAccess.objects.filter(user_id=user.pk).first()
        if profile is not None:
            if profile.is_owner and profile.is_admin:
                return
            self.is_owner = profile.is_owner
            self.is_admin = profile.is_admin
            self.role = profile.role
            self.role_label = profile.role_label
            # Employees use explicit grants; administrators receive working rights from their role.
            values = profile.permissions if isinstance(profile.permissions, list) else []
            self.permissions = (frozenset(PERMISSIONS) if self.is_admin else
                                frozenset(v for v in values if isinstance(v, str) and v in PERMISSIONS))

    def can(self, key):
        return key in PERMISSIONS and (self.is_owner or key in self.permissions)

    @property
    def can_manage_staff(self):
        return self.is_owner or self.is_admin

    def can_manage_account(self, account):
        try:
            profile = account.account_access
        except ObjectDoesNotExist:
            return False
        if self.is_owner:
            return not profile.is_owner
        return self.is_admin and not profile.is_owner and not profile.is_admin

    @property
    def can_manage_thresholds(self):
        return self.is_owner or self.is_admin

    def can_reset_sessions_for(self, account):
        return account.is_active and (self.can_manage_account(account)
            or (self.is_owner and account.pk == self.user.pk))

    def __getattr__(self, name):
        if '_' in name:
            return self.can(name.replace('_', '.', 1))
        raise AttributeError(name)

    @property
    def archive_view(self):
        return any(self.can(f'{kind}.view') for kind in ('dishes', 'preparations', 'ingredients', 'suppliers'))

    @property
    def scenarios_view(self):
        return all(self.can(key) for key in ('prices.view', 'ingredients.view', 'dishes.view', 'preparations.view', 'suppliers.view'))

    @property
    def scenarios_change(self):
        return self.scenarios_view and self.can('prices.add') and self.can('prices.change')

    @property
    def home_url(self):
        for key, route in (('dishes.view', 'dashboard'), ('preparations.view', 'preparations'),
                           ('ingredients.view', 'ingredients'), ('purchases.view', 'purchase-list'),
                           ('suppliers.view', 'suppliers'), ('journal.view', 'journal')):
            if self.can(key):
                return reverse(route)
        if self.can_manage_staff:
            return reverse('employee-list')
        return reverse('access-unavailable')


def is_owner(user):
    return Access(user).is_owner


def access_context(request):
    return {'access': getattr(request, 'access', None) or Access(request.user)}


OWNER_ROUTES = {'exchange', 'export-json', 'export-xlsx', 'restore-review', 'restore-download', 'restore-refresh'}
STAFF_ROUTES = {'employee-list', 'employee-create', 'employee-edit', 'employee-disable', 'employee-reset-sessions'}
ROUTE_PERMISSIONS = {
    'dashboard': 'dishes.view', 'dish-detail': 'dishes.view', 'dish-create': 'dishes.add', 'dish-edit': 'dishes.change',
    'preparations': 'preparations.view', 'preparation-detail': 'preparations.view',
    'preparation-create': 'preparations.add', 'preparation-edit': 'preparations.change',
    'ingredients': 'ingredients.view', 'ingredient-detail': 'ingredients.view',
    'ingredient-create': 'ingredients.add', 'ingredient-edit': 'ingredients.change',
    'suppliers': 'suppliers.view', 'supplier-create': 'suppliers.add', 'supplier-edit': 'suppliers.change',
    'price-list': 'prices.view', 'price-create': 'prices.add', 'price-apply': 'prices.change',
    'price-correct': 'prices.change', 'price-delete': 'prices.delete',
    'purchase-list': 'purchases.view', 'purchase-detail': 'purchases.view',
    'purchase-create': 'purchases.add', 'purchase-edit': 'purchases.change', 'purchase-line-delete': 'purchases.delete',
    'delivery-list': 'purchases.view', 'delivery-create': 'purchases.add', 'delivery-edit': 'purchases.change',
    'delivery-delete': 'purchases.delete', 'delivery-attach': 'purchases.change', 'journal': 'journal.view',
}


def authorize(request, route, kwargs):
    access = request.access
    if route in {'health', 'login', 'logout', 'access-unavailable'}:
        return
    if route in OWNER_ROUTES:
        allowed = access.is_owner
    elif route in STAFF_ROUTES:
        allowed = access.can_manage_staff
    elif route == 'thresholds':
        allowed = access.can_manage_thresholds
    elif route == 'archive':
        allowed = access.archive_view
    elif route == 'dish-transition':
        action = kwargs.get('action')
        allowed = action in {'archive', 'trash', 'restore', 'activate'} and access.can(
            'dishes.change' if action in {'restore', 'activate'} else 'dishes.delete')
    elif route == 'catalog-transition':
        kind = {'ingredient': 'ingredients', 'preparation': 'preparations', 'supplier': 'suppliers'}.get(kwargs.get('kind'))
        action = kwargs.get('action')
        allowed = kind is not None and action in {'trash', 'restore', 'purge'} and access.can(
            f'{kind}.change' if action == 'restore' else f'{kind}.delete')
    elif route == 'scenario-editor':
        allowed = access.scenarios_view
        if request.method == 'POST' and request.POST.get('intent') == 'confirm':
            allowed = allowed and access.scenarios_change
        if request.GET.get('purchase'):
            allowed = allowed and access.can('purchases.view')
    else:
        allowed = route in ROUTE_PERMISSIONS and access.can(ROUTE_PERMISSIONS[route])
    if route == 'price-create' and request.method == 'POST' and request.POST.get('apply_now') not in (None, '', 'False', 'false'):
        allowed = allowed and access.can('prices.change')
    if not allowed:
        raise PermissionDenied('Для этого действия нет разрешения. Обратитесь к администратору.')


def visible_dependencies(access, dependencies):
    visible, hidden = [], False
    for description in dependencies:
        # ValidationError can wrap or combine dependency descriptions. Treat
        # the whole message as private when any embedded reference is hidden;
        # splitting lines could expose the tail of a multiline object name.
        required = {permission for prefix, permission in (
            ('Блюдо:', 'dishes.view'), ('Заготовка:', 'preparations.view'),
            ('Закупка', 'purchases.view'), ('Цена позиции:', 'prices.view'),
            ('Цены позиции:', 'prices.view')) if prefix in description}
        if any(not access.can(key) for key in required):
            hidden = True
        else:
            visible.append(description)
    if hidden:
        visible.append('Есть связанные записи в недоступных вам разделах. Обратитесь к администратору.')
    return visible
