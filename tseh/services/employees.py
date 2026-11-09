"""Owner-only employee changes with stale-form protection and session revocation."""
import re

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.models import Session
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from tseh.access import normalize_permissions
from tseh.models.accounts import AccountAccess, AccountEvent

SALT = "tseh-employee-change-v1"
TOKEN_LIFETIME = 3600
RESERVED_LOGINS = {"tseh", "tseh_staging"}


def canonical_username(value):
    if not isinstance(value, str):
        raise ValidationError("Введите логин сотрудника.")
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,149}", value):
        raise ValidationError("Логин: от 3 до 150 латинских букв, цифр, точек, дефисов или подчёркиваний. Начните с буквы или цифры.")
    if value in RESERVED_LOGINS:
        raise ValidationError("Этот логин зарезервирован для администратора.")
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
    return signing.dumps({"actor": actor.pk, "target": employee.pk if employee else None,
                          "version": profile.version if profile else None, "action": action}, salt=SALT)


def _check_token(token, actor, employee=None, *, action="save"):
    expected = {"actor": actor.pk, "target": employee.pk if employee else None,
                "version": employee.account_access.version if employee else None, "action": action}
    try:
        supplied = signing.loads(token, salt=SALT, max_age=TOKEN_LIFETIME)
        if supplied == expected:
            return
    except (signing.BadSignature, ValueError, TypeError):
        pass
    raise ValidationError("Форма доступа устарела или недействительна. Откройте её заново и проверьте изменения.")


def _lock_owner(actor):
    if not getattr(actor, "is_authenticated", False):
        raise PermissionDenied
    profile = AccountAccess.objects.select_for_update().filter(user_id=actor.pk).first()
    if profile is None or not profile.is_owner:
        raise PermissionDenied
    current = get_user_model().objects.get(pk=actor.pk)
    if not current.is_active:
        raise PermissionDenied
    return current


def _lock_employee(pk):
    profile = AccountAccess.objects.select_for_update().filter(user_id=pk).first()
    if profile is None or profile.is_owner:
        raise PermissionDenied("Доступ администратора нельзя менять через список сотрудников.")
    employee = get_user_model().objects.select_for_update().get(pk=pk)
    employee.account_access = profile
    return employee


def _snapshot(employee):
    return {"user_id": employee.pk, "username": employee.username, "display_name": employee.first_name,
            "is_active": employee.is_active, "permissions": list(employee.account_access.permissions),
            "version": employee.account_access.version}


def revoke_sessions(user_id):
    keys = [session.session_key for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator()
            if session.get_decoded().get("_auth_user_id") == str(user_id)]
    Session.objects.filter(session_key__in=keys).delete()
    return len(keys)


@sensitive_variables("password1", "password2")
def save_employee(actor, *, token, username, display_name="", permissions=(), password1="", password2="", employee_id=None):
    with transaction.atomic():
        owner = _lock_owner(actor)
        employee = _lock_employee(employee_id) if employee_id is not None else None
        if employee is not None and not employee.is_active:
            raise ValidationError("Сотрудник отключён. Изменение доступа недоступно.")
        _check_token(token, owner, employee)
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
                    profile = AccountAccess.objects.create(user=employee, permissions=permissions)
                else:
                    profile = employee.account_access
                    profile.permissions = permissions
                    profile.version += 1
                    profile.save(update_fields=["permissions", "version"])
        except IntegrityError as exc:
            raise ValidationError({"username": "Логин уже занят. Выберите другой."}) from exc
        employee.account_access = profile
        after = _snapshot(employee)
        credential_or_rights_change = (not is_new and (bool(password1) or before["username"] != username
                                                     or before["permissions"] != permissions))
        if credential_or_rights_change:
            revoke_sessions(employee.pk)
        AccountEvent.objects.create(actor_name=owner.username, target_name=username,
            action="create" if is_new else "update", before=before,
            after={**after, "password_changed": bool(password1)})
        return employee


def disable_employee(actor, *, employee_id, token):
    with transaction.atomic():
        owner = _lock_owner(actor)
        employee = _lock_employee(employee_id)
        _check_token(token, owner, employee, action="disable")
        if not employee.is_active:
            raise ValidationError("Сотрудник уже отключён.")
        before = _snapshot(employee)
        employee.is_active = False
        employee.save(update_fields=["is_active"])
        profile = employee.account_access
        profile.version += 1
        profile.save(update_fields=["version"])
        revoke_sessions(employee.pk)
        AccountEvent.objects.create(actor_name=owner.username, target_name=employee.username,
                                    action="disable", before=before, after=_snapshot(employee))
        return employee
