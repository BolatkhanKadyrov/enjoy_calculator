from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods

from tseh.access import is_owner
from tseh.employee_forms import EmployeeForm, DisableEmployeeForm
from tseh.models.accounts import AccountEvent
from tseh.services.employees import save_employee, disable_employee


def owner_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not is_owner(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


def _employee(pk):
    return get_object_or_404(get_user_model().objects.select_related("account_access"),
                             pk=pk, account_access__is_owner=False, is_active=True)


@owner_required
@require_GET
def employee_list(request):
    status = request.GET.get("status", "active")
    if status not in {"active", "inactive", "all"}:
        status = "active"
    query = get_user_model().objects.filter(account_access__is_owner=False).select_related("account_access")
    if status != "all":
        query = query.filter(is_active=status == "active")
    return render(request, "tseh/employee_list.html", {"employees": query.order_by("username", "pk"),
        "events": AccountEvent.objects.all()[:20], "owner": request.user, "status": status, "active": "employees"})


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
        "permission_groups": form.permission_groups, "active": "employees"}, status=400 if form.errors else 200)


@owner_required
@sensitive_post_parameters("password1", "password2")
@require_http_methods(["GET", "POST"])
def employee_create(request):
    return _editor(request)


@owner_required
@sensitive_post_parameters("password1", "password2")
@require_http_methods(["GET", "POST"])
def employee_edit(request, pk):
    return _editor(request, _employee(pk))


@owner_required
@require_http_methods(["GET", "POST"])
def employee_disable(request, pk):
    employee = _employee(pk)
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
