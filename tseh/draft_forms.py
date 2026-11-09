"""Permissive editing forms; publication uses the strict domain validators."""
from django import forms
from django.forms import BaseFormSet, formset_factory

from tseh.models import DishLine, Ingredient


KINDS = (("ingredient", "Сырьё и упаковка"), ("preparation", "Заготовка"), ("dish", "Блюдо"))
SECTIONS = {"ingredient": "ingredients", "preparation": "preparations", "dish": "dishes"}
PAYLOAD_FIELDS = {
    "ingredient": ("unit", "ingredient_kind", "loss", "supplier", "price_date", "pack_size", "pack_price", "zero_price_confirmed"),
    "preparation": ("output_quantity", "output_unit"),
    "dish": ("base_price", "green_override", "orange_override"),
}


def text_field(label, *, help_text="", max_length=120):
    return forms.CharField(label=label, required=False, max_length=max_length, strip=False,
                           help_text=help_text, widget=forms.TextInput(attrs={"autocomplete": "off"}))


class DraftListForm(forms.Form):
    q = forms.CharField(label="Название", max_length=160, required=False,
        widget=forms.TextInput(attrs={"type": "search", "placeholder": "Найти черновик"}))
    kind = forms.ChoiceField(label="Тип", required=False, choices=(("", "Все типы"),) + KINDS)
    state = forms.ChoiceField(label="Состояние", required=False,
        choices=(("draft", "Черновики"), ("published", "Опубликованные"), ("all", "Все")))
    sort = forms.ChoiceField(label="Сортировка", required=False, choices=(
        ("updated", "Недавно изменённые"), ("name", "Название: А–Я"), ("-name", "Название: Я–А"), ("oldest", "Сначала старые")))

    def __init__(self, *args, allowed_kinds, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["kind"].choices = [("", "Все типы")] + [(key, label) for key, label in KINDS if key in allowed_kinds]


class DraftForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    name = forms.CharField(label="Название", max_length=160)
    notes = forms.CharField(label="Что нужно уточнить", required=False, max_length=8000, strip=False,
        widget=forms.Textarea(attrs={"rows": 3}), help_text="Вопросы повару, недостающие данные и ваши пояснения.")

    def __init__(self, *args, kind, draft=None, token, supplier_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind
        self.initial.setdefault("token", token)
        fields = {
            "unit": forms.ChoiceField(label="Базовая единица", required=False, choices=[("", "Не выбрана")] + Ingredient.UNITS),
            "ingredient_kind": forms.ChoiceField(label="Тип позиции", required=False,
                choices=(("", "Не выбран"), ("ingredient", "Сырьё"), ("packaging", "Упаковка"))),
            "loss": text_field("Потери при первичной обработке, %", help_text="Если потерь нет, укажите 0. Пустое поле означает, что потери ещё не уточнены."),
            "supplier": forms.ChoiceField(label="Поставщик (необязательно)", required=False,
                choices=[("", "Не указан")] + list(supplier_choices)),
            "price_date": text_field("Дата цены", help_text="Формат: ГГГГ-ММ-ДД."),
            "pack_size": text_field("Количество в одной упаковке", help_text="В выбранной базовой единице. Можно оставить пустым до уточнения фасовки."),
            "pack_price": text_field("Цена одной упаковки, ₸", help_text="Оставьте пустым, если цена неизвестна. Пустое поле не считается нулевой ценой."),
            "zero_price_confirmed": forms.BooleanField(required=False, label="Подтверждаю, что товар действительно бесплатный"),
            "output_quantity": text_field("Выход после приготовления", help_text="Количество готовой заготовки из одного замеса. Для публикации потребуется точное число."),
            "output_unit": forms.ChoiceField(label="Единица выхода", required=False,
                choices=[("", "Не выбрана")] + DishLine.UNITS, widget=forms.Select(attrs={"id": "id_draft_output_unit"})),
            "base_price": text_field("Отпускная цена за порцию, ₸", help_text="Цена цеха для кофейни. Можно заполнить позже."),
            "green_override": text_field("Собственный зелёный порог, %", help_text="Необязательно: пустое поле использует общий порог."),
            "orange_override": text_field("Собственный оранжевый порог, %", help_text="Необязательно: пустое поле использует общий порог."),
        }
        for key in PAYLOAD_FIELDS[kind]:
            self.fields[key] = fields[key]
        if draft:
            self.initial.update(name=draft.name, notes=draft.notes, **{key: draft.payload.get(key, "") for key in PAYLOAD_FIELDS[kind]})
        else:
            self.fields["source_text"] = forms.CharField(label="Исходный текст (необязательно)", required=False, strip=False,
                max_length=20000, widget=forms.Textarea(attrs={"rows": 5}),
                help_text="После первого сохранения исходник останется неизменным. Уточнения вносите в поля и заметки.")
        for key in ("unit", "output_unit", "supplier"):
            if key not in self.fields:
                continue
            value = self.data.get(self.add_prefix(key), "") if self.is_bound else self.initial.get(key, "")
            known = {key for key, _ in self.fields[key].choices}
            # Keep unknown imported strings visible; saving a draft must not erase them.
            if value and value not in known and isinstance(value, str) and len(value) <= 160:
                label = "Поставщик недоступен — прежняя связь сохранена" if key == "supplier" else "Исходное значение: " + value
                self.fields[key].choices = list(self.fields[key].choices) + [(value, label)]

    def payload(self):
        return {key: self.cleaned_data.get(key, False if key == "zero_price_confirmed" else "") for key in PAYLOAD_FIELDS[self.kind]}


class DraftLineForm(forms.Form):
    component = forms.ChoiceField(label="Связанный компонент", required=False, choices=(),
        widget=forms.Select(attrs={"data-component-unit-bound": "1"}))
    label = forms.CharField(label="Название компонента", required=False, max_length=500, strip=False,
        help_text="Сохранится, даже если компонент пока не выбран.")
    quantity = text_field("Количество", help_text="Можно сохранить диапазон или вопрос; перед публикацией нужно точное число.", max_length=120)
    unit = forms.ChoiceField(label="Единица", required=False, choices=[("", "Не выбрана")] + DishLine.UNITS)
    note = forms.CharField(label="Заметка к строке", required=False, max_length=1024, strip=False,
                           widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, component_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["component"].choices = [("", "Пока не выбран")] + list(component_choices)
        # Draft quantities can be ranges: the strict recipe unit converter must not alter them.
        self.fields["unit"].widget.attrs["id"] = "id_" + self.add_prefix("measure")
        unit = self.data.get(self.add_prefix("unit"), "") if self.is_bound else self.initial.get("unit", "")
        if unit and unit not in dict(DishLine.UNITS) and isinstance(unit, str) and len(unit) <= 32:
            self.fields["unit"].choices = list(self.fields["unit"].choices) + [(unit, "Исходное значение: " + unit)]
        # Existing unresolved references remain explicit until the editor replaces them.
        reference = self.initial.get("component", "")
        known = {value for _group, options in component_choices for value, _label in options}
        if reference and reference not in known:
            self.fields["component"].choices = list(self.fields["component"].choices) + [(reference, "Компонент недоступен — уточните связь")]


class DraftBaseFormSet(BaseFormSet):
    deletion_widget = forms.HiddenInput


DraftLineFormSet = formset_factory(DraftLineForm, formset=DraftBaseFormSet, extra=0, can_delete=True,
                                  max_num=100, validate_max=True, absolute_max=100)


class DraftConfirmForm(forms.Form):
    preview_token = forms.CharField(widget=forms.HiddenInput)
    confirm = forms.BooleanField(label="Опубликовать карточку и включить её в расчёты")


class DraftDeleteForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    confirm = forms.BooleanField(label="Удалить черновик и его исходный текст без восстановления")
