from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.forms import BaseFormSet, formset_factory
from django.utils import timezone

from tseh.calculation import FACTORS, decimal_display, normalized
from fractions import Fraction
from tseh.models import DishLine, Ingredient, Preparation, Supplier


class ExactDecimalField(forms.DecimalField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("localize", True)
        kwargs.setdefault("widget", forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"}))
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        if isinstance(value, Decimal):
            value = format(value, "f")
            if "." in value:
                value = value.rstrip("0").rstrip(".")
        return super().prepare_value(value)


class CommandForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)


def convert_bound_quantity(form, data, quantity_name, unit_name, basis_name):
    """Fallback for forms submitted without JS: preserve the quantity's declared unit."""
    source, target = data.get(basis_name), data.get(unit_name)
    value = data.get(quantity_name)
    if value is None or not target:
        return
    if source and source != target:
        if source not in FACTORS:
            raise ValidationError("Неизвестная исходная единица количества.")
        if FACTORS[source][0] == FACTORS[target][0]:
            value = decimal_display(Fraction(value) * Fraction(FACTORS[source][1]) / Fraction(FACTORS[target][1]))
            data[quantity_name] = form.fields[quantity_name].clean(value)
    if form.is_bound:
        form.data = form.data.copy()
        form.data[form.add_prefix(quantity_name)] = format(value, "f")
        form.data[form.add_prefix(basis_name)] = target
    data[basis_name] = target


class SupplierForm(CommandForm):
    name = forms.CharField(label="Название поставщика", max_length=160)
    note = forms.CharField(label="Контакты или заметка", max_length=2000, required=False, widget=forms.Textarea(attrs={"rows": 3}))


class IngredientEditForm(CommandForm):
    name = forms.CharField(label="Название позиции", max_length=160)
    loss = ExactDecimalField(label="Потери при первичной обработке, %", max_digits=11, decimal_places=8,
                             min_value=0, max_value=Decimal("99.99999999"), initial=0,
                             help_text="Очистка и разделка. Потери при приготовлении сюда не входят.")

    def __init__(self, *args, ingredient=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_packaging = ingredient is not None and ingredient.kind == "packaging"
        if self.is_packaging:
            self.initial["loss"] = Decimal(0)
            self.fields["loss"].disabled = True
            self.fields["loss"].help_text = "Для упаковки потери всегда равны 0%."

    def clean(self):
        data = super().clean()
        if self.is_packaging and self.is_bound and self.add_prefix("loss") in self.data:
            try:
                supplied = self.fields["loss"].clean(self.data.get(self.add_prefix("loss")))
            except ValidationError:
                supplied = None
            if supplied != 0:
                self.add_error("loss", "Для упаковки потери должны быть равны 0%.")
        return data


class IngredientCreateForm(IngredientEditForm):
    kind = forms.ChoiceField(label="Тип позиции", choices=[("ingredient", "Ингредиент"), ("packaging", "Упаковка")], required=False, initial="ingredient")
    unit = forms.ChoiceField(label="Базовая единица", choices=Ingredient.UNITS)
    supplier = forms.ModelChoiceField(label="Поставщик (необязательно)", queryset=Supplier.objects.filter(status="active"),
                                     empty_label="Не указан", required=False)
    pack_size = ExactDecimalField(label="Содержимое одной упаковки", max_digits=24, decimal_places=12,
                                  min_value=Decimal("0.000000000001"), help_text="В выбранной базовой единице: например, 5 для упаковки 5 кг.")
    pack_price = ExactDecimalField(label="Цена одной упаковки, ₸", max_digits=14, decimal_places=2, min_value=0)
    price_date = forms.DateField(label="Дата цены", initial=timezone.localdate,
                                widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}), input_formats=["%Y-%m-%d"])
    confirm_zero = forms.BooleanField(label="Подтверждаю нулевую закупочную цену", required=False,
                                      help_text="Отметьте только если товар действительно бесплатный: нулевая цена снизит себестоимость.")

    def clean(self):
        data = super().clean()
        if data.get("pack_price") == 0 and not data.get("confirm_zero"):
            self.add_error("pack_price", "Нулевая цена снизит себестоимость. Подтвердите её отдельной отметкой.")
        data["kind"] = data.get("kind") or "ingredient"
        if data["kind"] == "packaging" and data.get("loss") != 0:
            self.add_error("loss", "Для упаковки потери должны быть равны 0%.")
        return data


class DishForm(CommandForm):
    name = forms.CharField(label="Название блюда", max_length=160)
    base_price = ExactDecimalField(label="Отпускная цена за порцию, ₸", max_digits=14, decimal_places=2, min_value=0,
                                   help_text="Цена цеха для кофейни.")
    green_mode = forms.ChoiceField(label="Зелёный порог", choices=[("default", "По умолчанию"), ("custom", "Собственное значение")], required=False, initial="default")
    green_value = ExactDecimalField(label="Собственный зелёный порог, %", max_digits=20, decimal_places=8, min_value=Decimal("0.00000001"), required=False)
    orange_mode = forms.ChoiceField(label="Оранжевый порог", choices=[("default", "По умолчанию"), ("custom", "Собственное значение")], required=False, initial="default")
    orange_value = ExactDecimalField(label="Собственный оранжевый порог, %", max_digits=20, decimal_places=8, min_value=Decimal("0.00000001"), required=False)

    def clean(self):
        data = super().clean()
        for name in ("green", "orange"):
            if data.get(f"{name}_mode") == "custom":
                if data.get(f"{name}_value") is None:
                    self.add_error(f"{name}_value", "Укажите собственный порог.")
                data[f"{name}_override"] = data.get(f"{name}_value")
            else:
                data[f"{name}_override"] = None
        return data


class PreparationForm(CommandForm):
    name = forms.CharField(label="Название заготовки", max_length=160)
    output_quantity = ExactDecimalField(label="Выход после приготовления", max_digits=30, decimal_places=15,
                                       min_value=Decimal("0.000000000000001"), help_text="Количество готовой заготовки из одного замеса.")
    output_unit = forms.ChoiceField(label="Единица", choices=DishLine.UNITS, initial="kg")
    output_input_unit = forms.CharField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["output_input_unit"] = self.initial.get("output_unit", "kg")

    def clean(self):
        data = super().clean()
        convert_bound_quantity(self, data, "output_quantity", "output_unit", "output_input_unit")
        if data.get("output_quantity") is not None and data.get("output_unit"):
            try:
                normalized(data["output_quantity"], data["output_unit"], FACTORS[data["output_unit"]][0])
            except ValueError as exc:
                raise ValidationError(str(exc))
        return data


class ThresholdForm(CommandForm):
    green = ExactDecimalField(label="Верхняя граница зелёной зоны, %", max_digits=20, decimal_places=8, min_value=Decimal("0.00000001"), required=False)
    orange = ExactDecimalField(label="Верхняя граница оранжевой зоны, %", max_digits=20, decimal_places=8, min_value=Decimal("0.00000001"), required=False)

    def clean(self):
        data = super().clean()
        if data.get("green") is not None and data.get("orange") is not None and data["green"] >= data["orange"]:
            raise ValidationError("Зелёный порог должен быть меньше оранжевого.")
        return data


class DiscountForm(forms.Form):
    discount = ExactDecimalField(label="Общая скидка, %", max_digits=11, decimal_places=8, min_value=0, max_value=Decimal("99.99999999"), initial=0,
                                help_text="Скидка только для анализа. Базовые отпускные цены не меняются.")


class ComponentSelect(forms.Select):
    """Display hints only; the selected model still determines server-side units."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.component_units = {}

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        if value and str(value) in self.component_units:
            option["attrs"]["data-unit"] = self.component_units[str(value)]
        return option


class ComponentChoiceField(forms.ChoiceField):
    """A typed, grouped choice resolving to either supported component model."""

    def clean(self, value):
        key = super().clean(value)
        return self.objects[key] if key else None


class LineForm(forms.Form):
    component = ComponentChoiceField(label="Компонент", required=False, widget=ComponentSelect)
    ingredient = forms.ModelChoiceField(queryset=Ingredient.objects.filter(status="active").select_related("current_price"), required=False, widget=forms.HiddenInput)
    preparation = forms.ModelChoiceField(queryset=Preparation.objects.filter(status="active"), required=False, widget=forms.HiddenInput)
    quantity = ExactDecimalField(label="Количество", max_digits=30, decimal_places=15, min_value=Decimal("0.000000000000001"))
    unit = forms.ChoiceField(label="Единица", choices=DishLine.UNITS, initial="g")
    quantity_unit = forms.CharField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, preparation_recipe=False, component_objects=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["quantity_unit"] = self.initial.get("unit", "g")
        if preparation_recipe:
            self.fields["ingredient"].queryset = Ingredient.objects.filter(kind="ingredient", status="active").select_related("current_price")
        # Per-form data contains only this row, not a copy of the entire 100-row POST.
        if self.is_bound:
            names = set(self.fields) | {"DELETE", "ORDER"}
            self.data = {self.add_prefix(name): self.data.get(self.add_prefix(name))
                         for name in names if self.add_prefix(name) in self.data}
        if component_objects is None:
            component_objects = (list(self.fields["ingredient"].queryset), list(self.fields["preparation"].queryset))
        groups = {"Сырьё": [], "Упаковка": [], "Заготовки": []}
        objects = {}
        for obj in component_objects[0]:
            key = f"ingredient:{obj.pk}"
            objects[key] = obj
            groups["Упаковка" if obj.kind == "packaging" else "Сырьё"].append((key, obj.name))
        for obj in component_objects[1]:
            key = f"preparation:{obj.pk}"
            objects[key] = obj
            groups["Заготовки"].append((key, obj.name))
        self.fields["component"].objects = objects
        self.fields["component"].widget.component_units = {
            key: {"kg": "g", "l": "ml", "pc": "pc"}[obj.unit] for key, obj in objects.items()
        }
        self.fields["component"].choices = [("", "Выберите компонент")] + [
            (label, choices) for label, choices in groups.items() if choices
        ]
        self.component_submitted = self.is_bound and self.add_prefix("component") in self.data
        if self.component_submitted:
            self.fields["component"].required = True
            # Old hidden values must neither override nor invalidate an explicit choice.
            self.data = self.data.copy()
            self.data[self.add_prefix("ingredient")] = ""
            self.data[self.add_prefix("preparation")] = ""
        else:
            source = self.data if self.is_bound else self.initial
            selected = [(kind, source.get(self.add_prefix(kind) if self.is_bound else kind))
                        for kind in ("ingredient", "preparation")]
            selected = [(kind, value) for kind, value in selected if value]
            if len(selected) == 1:
                kind, value = selected[0]
                key = f"{kind}:{getattr(value, 'pk', value)}"
                if self.is_bound:
                    self.data = self.data.copy()
                    self.data[self.add_prefix("component")] = key
                else:
                    self.initial["component"] = key

    def clean(self):
        data = super().clean()
        convert_bound_quantity(self, data, "quantity", "unit", "quantity_unit")
        if self.component_submitted:
            component = data.get("component")
            data["ingredient"] = component if isinstance(component, Ingredient) else None
            data["preparation"] = component if isinstance(component, Preparation) else None
            if component is None:
                return data
            for kind in ("ingredient", "preparation"):
                self.data[self.add_prefix(kind)] = str(data[kind].pk) if data[kind] else ""
        if bool(data.get("ingredient")) == bool(data.get("preparation")):
            raise ValidationError("Выберите один ингредиент или одну заготовку в строке.")
        component = data.get("ingredient") or data.get("preparation")
        if component and data.get("quantity") is not None and data.get("unit"):
            try:
                normalized(data["quantity"], data["unit"], component.unit)
            except ValueError as exc:
                raise ValidationError(str(exc))
        return data


class RecipeFormSet(BaseFormSet):
    deletion_widget = forms.HiddenInput

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        if not hasattr(self, "component_objects"):
            ingredients = Ingredient.objects.filter(status="active").select_related("current_price")
            if kwargs.get("preparation_recipe"):
                ingredients = ingredients.filter(kind="ingredient")
            self.component_objects = (list(ingredients), list(Preparation.objects.filter(status="active")))
        kwargs["component_objects"] = self.component_objects
        return kwargs

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        if not any(form.cleaned_data and not form.cleaned_data.get("DELETE") for form in self.forms):
            raise ValidationError("Добавьте хотя бы один компонент.")


LineFormSet = formset_factory(LineForm, formset=RecipeFormSet, extra=0, min_num=1, validate_min=True,
                              max_num=100, validate_max=True, absolute_max=100, can_delete=True)
