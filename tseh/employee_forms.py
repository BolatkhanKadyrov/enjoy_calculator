from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from tseh.access import PERMISSIONS, PERMISSION_GROUPS, normalize_permissions
from tseh.services.employees import available_username, employee_token


class EmployeeForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
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
        self.fields["permissions"].choices = list(PERMISSIONS.items())
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
            return normalize_permissions(self.cleaned_data["permissions"])
        except (ValueError, TypeError) as exc:
            raise ValidationError("Выберите только разрешения из списка.") from exc

    def clean(self):
        data = super().clean()
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
    def permission_groups(self):
        values = self["permissions"].value() or []
        selected = set(values)
        return [{**group, "options": [
            {"key": key, "label": PERMISSIONS[key], "id": "permission_" + key.replace(".", "_"),
             "checked": key in selected} for key in group["permissions"]
        ]} for group in PERMISSION_GROUPS]


class DisableEmployeeForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    confirm = forms.BooleanField(label="Отключить вход сотруднику и завершить его сеансы")

    def __init__(self, *args, actor, employee, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("token", employee_token(actor, employee, action="disable"))
