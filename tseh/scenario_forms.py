"""Scenario input stays separate from the composition of source purchases."""
from django import forms
from django.core.exceptions import ValidationError
from django.forms import BaseFormSet, formset_factory

from tseh.models import Ingredient, Price, Supplier
from tseh.price_forms import PriceDataForm, ReplacementPriceField


class ObjectChoiceField(forms.ChoiceField):
    """Resolve a choice from a consistent prefetched snapshot without per-row queries."""
    def __init__(self, *args, objects=(), labels=None, empty_label="Выберите", **kwargs):
        self.objects = {str(obj.pk): obj for obj in objects}
        labels = labels or {}
        choices = [("", empty_label)] + [(str(obj.pk), labels.get(str(obj.pk), str(obj))) for obj in objects]
        super().__init__(*args, choices=choices, **kwargs)

    def clean(self, value):
        key = super().clean(value)
        return self.objects[key] if key else None


def choice_catalog():
    ingredients = list(Ingredient.objects.filter(status="active").select_related("current_price__supplier"))
    suppliers = list(Supplier.objects.filter(status="active"))
    prices = list(Price.objects.filter(ingredient__status="active").select_related("ingredient", "supplier").order_by("-price_date", "-created_at", "id"))
    labeler = ReplacementPriceField(queryset=Price.objects.none())
    return {"ingredients": ingredients, "suppliers": suppliers, "prices": prices,
        "ingredient_map": {str(obj.pk): obj for obj in ingredients},
        "ingredient_labels": {str(obj.pk): f"{obj.name} · {obj.get_unit_display()}" for obj in ingredients},
        "price_labels": {str(obj.pk): labeler.label_from_instance(obj) for obj in prices}}


class ScenarioRowForm(forms.Form):
    mode = forms.ChoiceField(label="Какую цену использовать", choices=[
        ("new", "Ввести новую цену"), ("existing", "Выбрать сохранённую цену")], initial="new")

    def __init__(self, *args, catalog=None, **kwargs):
        super().__init__(*args, **kwargs)
        catalog = catalog or choice_catalog()
        self.fields["ingredient"] = ObjectChoiceField(label="Сырьё или упаковка",
            objects=catalog["ingredients"], labels=catalog["ingredient_labels"], empty_label="Выберите позицию")
        self.order_fields(["ingredient", "mode"])
        selected = self.data.get(self.add_prefix("ingredient")) if self.is_bound else self.initial.get("ingredient")
        self.selected_ingredient = catalog["ingredient_map"].get(str(selected))
        available = [price for price in catalog["prices"] if self.selected_ingredient
            and price.ingredient_id == self.selected_ingredient.pk and price.pk != self.selected_ingredient.current_price_id]
        self.fields["saved_price"] = ObjectChoiceField(label="Сохранённая цена", objects=available,
            labels=catalog["price_labels"], required=False, empty_label="Выберите цену",
            help_text="Выберите позицию выше и нажмите «Показать сохранённые цены». Действующая цена в список не включена.")
        self.new_price_form = PriceDataForm(self.data if self.is_bound else None,
            prefix=f"{self.prefix}-new", ingredient=self.selected_ingredient, use_required_attribute=False)
        self.new_price_form.fields["supplier"] = ObjectChoiceField(label="Поставщик (необязательно)",
            objects=catalog["suppliers"], required=False, empty_label="Не указан")
        selected_mode = self.data.get(self.add_prefix("mode")) if self.is_bound else self.initial.get("mode", "new")
        if selected_mode == "existing":
            self.new_price_form._errors = {}
        else:
            # Preserve inactive input for a round trip without validating an unrelated
            # price against the newly selected ingredient. Switching to existing mode
            # configures and validates the real choice field on the next request.
            self.fields["saved_price"] = forms.CharField(required=False, widget=forms.HiddenInput)

    def clean(self):
        data = super().clean()
        if data.get("mode") == "existing" and not data.get("saved_price"):
            self.add_error("saved_price", "Выберите сохранённую цену или переключитесь на ввод новой.")
        if data.get("mode") == "new" and not self.new_price_form.is_valid():
            raise ValidationError("Проверьте поля новой цены ниже.")
        return data

    def row_data(self):
        data = self.cleaned_data
        if data["mode"] == "existing":
            return {"ingredient_id": data["ingredient"].pk, "price_id": data["saved_price"].pk}
        return {"ingredient_id": data["ingredient"].pk, "data": self.new_price_form.price_data(),
                "confirm_zero": self.new_price_form.cleaned_data["confirm_zero"]}


class BaseScenarioFormSet(BaseFormSet):
    def __init__(self, *args, **kwargs):
        self.catalog = choice_catalog()
        super().__init__(*args, **kwargs)

    def get_form_kwargs(self, index):
        return {**super().get_form_kwargs(index), "catalog": self.catalog}

    def _construct_form(self, index, **kwargs):
        kwargs["empty_permitted"] = False
        return super()._construct_form(index, **kwargs)

    def clean(self):
        if any(self.errors):
            return
        keys = [form.cleaned_data["ingredient"].pk for form in self.forms]
        if len(keys) != len(set(keys)):
            raise ValidationError("Оставьте одну строку изменения цены для каждой позиции.")


ScenarioFormSet = formset_factory(ScenarioRowForm, formset=BaseScenarioFormSet, extra=0,
    min_num=1, validate_min=True, max_num=100, validate_max=True, absolute_max=100)
