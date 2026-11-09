from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods

from tseh.access import Access
from tseh.employee_forms import EmployeeForm, DisableEmployeeForm, ResetEmployeeSessionsForm
from tseh.models.accounts import AccountEvent
from tseh.services.employees import save_employee, disable_employee, reset_employee_sessions


def manager_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not Access(request.user).can_manage_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


def _employee(request, pk, *, allow_owner_self=False):
    access = Access(request.user)
    query = get_user_model().objects.select_related("account_access").filter(is_active=True)
    if not (allow_owner_self and access.is_owner and request.user.pk == pk):
        query = query.filter(account_access__is_owner=False)
        if not access.is_owner:
            query = query.filter(account_access__is_admin=False)
    employee = get_object_or_404(query, pk=pk)
    if not (access.can_manage_account(employee) or
            (allow_owner_self and access.can_reset_sessions_for(employee))):
        raise PermissionDenied
    return employee


def _visible_events(access):
    if access.is_owner:
        return AccountEvent.objects.all()[:20]
    visible_ids = list(get_user_model().objects.filter(account_access__is_owner=False,
                      account_access__is_admin=False).values_list("pk", flat=True))
    events = AccountEvent.objects.filter(after__user_id__in=visible_ids)
    visible = []
    for event in events.iterator():
        # Promotion hides earlier events too; demotion must not reveal the admin period.
        if any((snapshot or {}).get("role") in {"owner", "admin"}
               for snapshot in (event.before, event.after)):
            continue
        visible.append(event)
        if len(visible) == 20:
            break
    return visible


@manager_required
@require_GET
def employee_list(request):
    status = request.GET.get("status", "active")
    if status not in {"active", "inactive", "all"}:
        status = "active"
    query = get_user_model().objects.filter(account_access__is_owner=False).select_related("account_access")
    access = Access(request.user)
    if not access.is_owner:
        query = query.filter(account_access__is_admin=False)
    if status != "all":
        query = query.filter(is_active=status == "active")
    employees = list(query.order_by("username", "pk"))
    for employee in employees:
        employee.can_manage = employee.is_active and access.can_manage_account(employee)
        employee.can_reset_sessions = access.can_reset_sessions_for(employee)
    owner = request.user if access.is_owner else None
    if owner is not None:
        owner.can_reset_sessions = True
    return render(request, "tseh/employee_list.html", {"employees": employees,
        "events": _visible_events(access), "owner": owner, "manager": request.user,
        "is_owner": access.is_owner, "status": status, "active": "employees"})


def _editor(request, employee=None):
    form = EmployeeForm(request.POST if request.method == "POST" else None, actor=request.user, employee=employee)
    if request.method == "POST" and form.is_valid():
        try:
            save_employee(request.user, **form.cleaned_data, employee_id=employee.pk if employee else None)
            messages.success(request, "Доступ сотрудника сохранён." if employee else "Сотрудник добавлен.")
            return redirect("employee-list")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "tseh/employee_form.html", {"form": form, "employee": employee,
        "title": "Изменить доступ сотрудника" if employee else "Добавить сотрудника",
        "permission_groups": form.permission_groups, "permissions_locked": form.permissions_locked,
        "selected_role": form.selected_role, "active": "employees"}, status=400 if form.errors else 200)


@manager_required
@sensitive_post_parameters("password1", "password2")
@require_http_methods(["GET", "POST"])
def employee_create(request):
    return _editor(request)


@manager_required
@sensitive_post_parameters("password1", "password2")
@require_http_methods(["GET", "POST"])
def employee_edit(request, pk):
    return _editor(request, _employee(request, pk))


@manager_required
@require_http_methods(["GET", "POST"])
def employee_disable(request, pk):
    employee = _employee(request, pk)
    form = DisableEmployeeForm(request.POST if request.method == "POST" else None, actor=request.user, employee=employee)
    if request.method == "POST" and form.is_valid():
        try:
            disable_employee(request.user, employee_id=employee.pk, token=form.cleaned_data["token"])
            messages.success(request, "Сотрудник отключён. Действующие входы завершены, история сохранена.")
            return redirect("employee-list")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "tseh/employee_disable.html", {"employee": employee, "form": form,
                  "active": "employees"}, status=400 if form.errors else 200)


@manager_required
@require_http_methods(["GET", "POST"])
def employee_reset_sessions(request, pk):
    employee = _employee(request, pk, allow_owner_self=True)
    is_self = employee.pk == request.user.pk
    form = ResetEmployeeSessionsForm(request.POST if request.method == "POST" else None,
                                     actor=request.user, employee=employee)
    if request.method == "POST" and form.is_valid():
        try:
            revoked = reset_employee_sessions(request.user, employee_id=employee.pk,
                                               token=form.cleaned_data["token"])
            if is_self:
                logout(request)
                messages.success(request, "Все ваши сеансы завершены. Войдите заново.")
                return redirect("login")
            messages.success(request, f"Завершены сеансы: {revoked}. Пароль и права аккаунта сохранены.")
            return redirect("employee-list")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "tseh/employee_reset_sessions.html", {"employee": employee, "form": form,
                  "is_self": is_self, "active": "employees"}, status=400 if form.errors else 200)
