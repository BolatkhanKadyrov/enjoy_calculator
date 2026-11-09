from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from tseh.access import Access, PERMISSIONS, PERMISSION_GROUPS, is_owner, normalize_permissions
from tseh.services.employees import available_username, employee_token


class EmployeeForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    role = forms.ChoiceField(label="Роль", required=False,
                             choices=(("employee", "Сотрудник"), ("admin", "Администратор")),
                             help_text="Администратор получает все рабочие права и управляет сотрудниками. Права ниже применяются только к роли «Сотрудник».")
    username = forms.CharField(label="Логин", max_length=150,
        help_text="Латинские буквы, цифры, точка, дефис или подчёркивание. Логин сохраняется строчными буквами.",
        widget=forms.TextInput(attrs={"autocomplete": "off", "autocapitalize": "none", "spellcheck": "false"}))
    display_name = forms.CharField(label="Имя сотрудника", max_length=150, required=False,
                                  widget=forms.TextInput(attrs={"autocomplete": "off"}))
    password1 = forms.CharField(label="Пароль", strip=False,
                               widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    password2 = forms.CharField(label="Повторите пароль", strip=False,
                               widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    permissions = forms.MultipleChoiceField(label="Права доступа", required=False,
                                            choices=(), widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, actor, employee=None, **kwargs):
        self.actor, self.employee = actor, employee
        super().__init__(*args, **kwargs)
        self.actor_access = Access(actor)
        if not self.actor_access.is_owner:
            self.fields.pop("role")
        else:
            self.initial.setdefault("role", employee.account_access.role if employee else "employee")
        self.fields["permissions"].choices = [(key, label) for key, label in PERMISSIONS.items()
                                              if self.actor_access.is_owner or key in self.actor_access.permissions]
        self.initial.setdefault("token", employee_token(actor, employee))
        if employee is not None:
            self.initial.update(username=employee.username, display_name=employee.first_name,
                                permissions=list(employee.account_access.permissions))
            for name in ("password1", "password2"):
                self.fields[name].required = False
            self.fields["password1"].label = "Новый пароль"
            self.fields["password1"].help_text = "Оставьте оба поля пароля пустыми, чтобы сохранить действующий пароль."
        else:
            self.fields["password1"].help_text = "Не менее 12 символов. Не используйте простой пароль или логин сотрудника."

    def clean_username(self):
        return available_username(self.cleaned_data["username"],
                                  employee_id=self.employee.pk if self.employee else None)

    def clean_permissions(self):
        try:
            values = normalize_permissions(self.cleaned_data["permissions"])
        except (ValueError, TypeError) as exc:
            raise ValidationError("Выберите только разрешения из списка.") from exc
        if not self.actor_access.is_owner and not set(values).issubset(self.actor_access.permissions):
            raise ValidationError("Нельзя назначить права шире собственных. Обратитесь к владельцу.")
        return values

    def clean_role(self):
        return self.cleaned_data.get("role") or self.initial.get("role", "employee")

    def clean(self):
        data = super().clean()
        if not is_owner(self.actor) and "role" in self.data:
            self.add_error(None, "Только владелец может назначать роль аккаунта.")
        first, second = data.get("password1", ""), data.get("password2", "")
        if first != second:
            self.add_error("password2", "Пароли не совпадают.")
        elif first:
            candidate = get_user_model()(username=data.get("username", ""), first_name=data.get("display_name", ""))
            try:
                validate_password(first, candidate)
            except ValidationError as exc:
                self.add_error("password1", exc)
        return data

    @property
    def selected_role(self):
        if "role" not in self.fields:
            return "employee"
        return self["role"].value() or self.initial.get("role", "employee")

    @property
    def permissions_locked(self):
        return self.selected_role == "admin"

    @property
    def permission_groups(self):
        values = self["permissions"].value() or []
        selected = set(values)
        allowed = {key for key, _label in self.fields["permissions"].choices}
        return [{**group, "options": [
            {"key": key, "label": PERMISSIONS[key], "id": "permission_" + key.replace(".", "_"),
             "checked": key in selected} for key in group["permissions"] if key in allowed
        ]} for group in PERMISSION_GROUPS if any(key in allowed for key in group["permissions"])]


class DisableEmployeeForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    confirm = forms.BooleanField(label="Отключить вход сотруднику и завершить его сеансы")

    def __init__(self, *args, actor, employee, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("token", employee_token(actor, employee, action="disable"))


class ResetEmployeeSessionsForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    confirm = forms.BooleanField(label="Завершить все сеансы этого аккаунта")

    def __init__(self, *args, actor, employee, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("token", employee_token(actor, employee, action="reset_sessions"))
