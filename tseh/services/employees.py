"""Role-aware account changes with stale-form protection and session revocation."""
import re

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.models import Session
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from tseh.access import Access, normalize_permissions
from tseh.models.accounts import AccountAccess, AccountEvent

SALT = "tseh-employee-change-v2"
TOKEN_LIFETIME = 3600
RESERVED_LOGINS = {"tseh", "tseh_staging"}


def canonical_username(value):
    if not isinstance(value, str):
        raise ValidationError("Введите логин сотрудника.")
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,149}", value):
        raise ValidationError("Логин: от 3 до 150 латинских букв, цифр, точек, дефисов или подчёркиваний. Начните с буквы или цифры.")
    if value in RESERVED_LOGINS:
        raise ValidationError("Этот логин зарезервирован для владельца.")
    return value


def available_username(value, *, employee_id=None):
    """A renamed account keeps its historical logins for unambiguous authorship."""
    username = canonical_username(value)
    current = get_user_model().objects.filter(username__iexact=username)
    previous = AccountEvent.objects.filter(target_name__iexact=username)
    if employee_id is not None:
        current = current.exclude(pk=employee_id)
        previous = previous.exclude(after__user_id=employee_id)
    if current.exists():
        raise ValidationError("Логин уже занят, в том числе отключённой учётной записью.")
    if previous.exists():
        raise ValidationError("Логин раньше использовался другим сотрудником и сохранён за ним для истории действий.")
    return username


def employee_token(actor, employee=None, *, action="save"):
    profile = employee.account_access if employee is not None else None
    return signing.dumps({"actor": actor.pk, "actor_version": actor.account_access.version,
                          "target": employee.pk if employee else None,
                          "version": profile.version if profile else None, "action": action}, salt=SALT)


def _check_token(token, actor, employee=None, *, action="save"):
    expected = {"actor": actor.pk, "actor_version": actor.account_access.version,
                "target": employee.pk if employee else None,
                "version": employee.account_access.version if employee else None, "action": action}
    try:
        supplied = signing.loads(token, salt=SALT, max_age=TOKEN_LIFETIME)
        if supplied == expected:
            return
    except (signing.BadSignature, ValueError, TypeError):
        pass
    raise ValidationError("Форма доступа устарела или недействительна. Откройте её заново и проверьте изменения.")


def _lock_manager(actor):
    if not getattr(actor, "is_authenticated", False):
        raise PermissionDenied
    # Every writer takes the same owners first, including calls outside middleware.
    list(AccountAccess.objects.select_for_update().filter(is_owner=True).order_by("pk"))
    profile = AccountAccess.objects.select_for_update().filter(user_id=actor.pk).first()
    if profile is None or not (profile.is_owner or profile.is_admin):
        raise PermissionDenied
    current = get_user_model().objects.get(pk=actor.pk)
    if not current.is_active:
        raise PermissionDenied
    current.account_access = profile
    return current


def _lock_employee(actor, pk, *, allow_owner_self=False):
    profile = AccountAccess.objects.select_for_update().filter(user_id=pk).first()
    if profile is None:
        raise PermissionDenied
    own_session_reset = allow_owner_self and actor.account_access.is_owner and pk == actor.pk
    if not own_session_reset and (profile.is_owner or
                                  (profile.is_admin and not actor.account_access.is_owner)):
        raise PermissionDenied("Недостаточно прав для управления этим аккаунтом.")
    employee = get_user_model().objects.select_for_update().get(pk=pk)
    employee.account_access = profile
    if not own_session_reset and not Access(actor, actor.account_access).can_manage_account(employee):
        raise PermissionDenied("Права этого сотрудника шире ваших. Обратитесь к владельцу.")
    return employee


def _check_assignable_permissions(manager, permissions):
    access = Access(manager, manager.account_access)
    if not access.is_owner and not set(permissions).issubset(access.permissions):
        raise PermissionDenied("Нельзя назначить сотруднику права шире собственных. Обратитесь к владельцу.")


def _snapshot(employee):
    return {"user_id": employee.pk, "username": employee.username, "display_name": employee.first_name,
            "is_active": employee.is_active, "permissions": list(employee.account_access.permissions),
            "role": employee.account_access.role, "version": employee.account_access.version}


def revoke_sessions(user_id):
    keys = [session.session_key for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator()
            if session.get_decoded().get("_auth_user_id") == str(user_id)]
    Session.objects.filter(session_key__in=keys).delete()
    return len(keys)


@sensitive_variables("password1", "password2")
def save_employee(actor, *, token, username, display_name="", permissions=(), password1="", password2="",
                  employee_id=None, role=None):
    with transaction.atomic():
        manager = _lock_manager(actor)
        employee = _lock_employee(manager, employee_id) if employee_id is not None else None
        if employee is not None and not employee.is_active:
            raise ValidationError("Сотрудник отключён. Изменение доступа недоступно.")
        _check_token(token, manager, employee)
        if role is not None and not manager.account_access.is_owner:
            raise PermissionDenied("Только владелец может назначать роль аккаунта.")
        selected_role = role if role is not None else (employee.account_access.role if employee else "employee")
        if not isinstance(selected_role, str) or selected_role not in {"employee", "admin"}:
            raise ValidationError({"role": "Выберите сотрудника или администратора."})
        try:
            username = available_username(username, employee_id=employee.pk if employee else None)
        except ValidationError as exc:
            raise ValidationError({"username": exc.messages}) from exc
        if not isinstance(display_name, str) or len(display_name.strip()) > 150:
            raise ValidationError({"display_name": "Имя сотрудника должно содержать не больше 150 символов."})
        display_name = display_name.strip()
        try:
            permissions = normalize_permissions(permissions)
        except (TypeError, ValueError) as exc:
            raise ValidationError({"permissions": "Выберите только разрешения из списка."}) from exc
        _check_assignable_permissions(manager, permissions)
        # Admin rights come only from the role. Demotion must not inherit an
        # implicit full grant through a persisted checkbox list.
        if selected_role == "admin":
            permissions = []
        User = get_user_model()
        is_new = employee is None
        if is_new:
            employee = User(username=username, first_name=display_name, is_active=True, is_staff=False, is_superuser=False)
            before = None
        else:
            before = _snapshot(employee)
        if not isinstance(password1, str) or not isinstance(password2, str):
            raise ValidationError({"password1": "Введите пароль."})
        if password1 != password2:
            raise ValidationError({"password2": "Пароли не совпадают."})
        if is_new and not password1:
            raise ValidationError({"password1": "Введите пароль нового сотрудника."})
        employee.username, employee.first_name = username, display_name
        if password1:
            validate_password(password1, employee)
            employee.set_password(password1)
        employee.full_clean()
        try:
            with transaction.atomic():
                employee.save()
                if is_new:
                    profile = AccountAccess.objects.create(user=employee, permissions=permissions,
                                                           is_admin=selected_role == "admin")
                else:
                    profile = employee.account_access
                    profile.permissions = permissions
                    profile.is_admin = selected_role == "admin"
                    profile.version += 1
                    profile.save(update_fields=["permissions", "is_admin", "version"])
        except IntegrityError as exc:
            raise ValidationError({"username": "Логин уже занят. Выберите другой."}) from exc
        employee.account_access = profile
        after = _snapshot(employee)
        credential_or_rights_change = (not is_new and (bool(password1) or before["username"] != username
                                                     or before["permissions"] != permissions
                                                     or before["role"] != selected_role))
        if credential_or_rights_change:
            revoke_sessions(employee.pk)
        AccountEvent.objects.create(actor_name=manager.username, target_name=username,
            action="create" if is_new else "update", before=before,
            after={**after, "password_changed": bool(password1)})
        return employee


def disable_employee(actor, *, employee_id, token):
    with transaction.atomic():
        manager = _lock_manager(actor)
        employee = _lock_employee(manager, employee_id)
        _check_token(token, manager, employee, action="disable")
        if not employee.is_active:
            raise ValidationError("Сотрудник уже отключён.")
        before = _snapshot(employee)
        employee.is_active = False
        employee.save(update_fields=["is_active"])
        profile = employee.account_access
        profile.version += 1
        profile.save(update_fields=["version"])
        revoke_sessions(employee.pk)
        AccountEvent.objects.create(actor_name=manager.username, target_name=employee.username,
                                    action="disable", before=before, after=_snapshot(employee))
        return employee


def reset_employee_sessions(actor, *, employee_id, token):
    """End current logins without disabling the account or changing credentials."""
    with transaction.atomic():
        manager = _lock_manager(actor)
        employee = _lock_employee(manager, employee_id, allow_owner_self=True)
        _check_token(token, manager, employee, action="reset_sessions")
        if not employee.is_active:
            raise ValidationError("Аккаунт отключён: его сеансы уже завершены.")
        before = _snapshot(employee)
        profile = employee.account_access
        profile.version += 1
        profile.save(update_fields=["version"])
        revoked = revoke_sessions(employee.pk)
        AccountEvent.objects.create(actor_name=manager.username, target_name=employee.username,
                                    action="reset_sessions", before=before,
                                    after={**_snapshot(employee), "revoked_sessions": revoked})
        return revoked
