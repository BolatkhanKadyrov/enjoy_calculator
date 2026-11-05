"""Exact, server validated forms for purchases and unallocated delivery."""
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.forms import BaseFormSet, formset_factory
from django.utils import timezone

from tseh.forms import CommandForm, ExactDecimalField
from tseh.models import Ingredient, Price, Purchase, Supplier
from tseh.price_forms import ReplacementPriceField


class PurchaseForm(CommandForm):
    supplier = forms.ModelChoiceField(label="Поставщик (необязательно)", queryset=Supplier.objects.filter(status="active"),
                                     required=False, empty_label="Не указан")
    purchase_date = forms.DateField(label="Дата закупки", initial=timezone.localdate,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}), input_formats=["%Y-%m-%d"])
    delivery_total = ExactDecimalField(label="Общая доставка, ₸", max_digits=14, decimal_places=2,
                                      min_value=0, initial=0)
    comment = forms.CharField(label="Комментарий (необязательно)", max_length=2000, required=False,
                              widget=forms.Textarea(attrs={"rows": 2}))
    reason = forms.CharField(label="Причина исправления", max_length=2000, required=False,
                             widget=forms.Textarea(attrs={"rows": 2}))
    confirm_zero = forms.BooleanField(label="Подтверждаю нулевые цены в составе закупки", required=False,
                                      help_text="Нулевая цена снизит себестоимость после её применения.")

    def __init__(self, *args, purchase=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.purchase = purchase
        if purchase is None:
            self.fields.pop("reason")
        else:
            self.fields["reason"].help_text = "Обязательна при изменении общей суммы доставки. Сохранится в журнале."

    def clean(self):
        data = super().clean()
        if (self.purchase and data.get("delivery_total") is not None
                and data["delivery_total"] != self.purchase.delivery_total and not data.get("reason")):
            self.add_error("reason", "Укажите причину исправления суммы доставки.")
        return data

    def purchase_data(self):
        data = self.cleaned_data
        return {"supplier_id": data["supplier"].pk if data["supplier"] else None,
                **{name: data[name] for name in ("purchase_date", "delivery_total", "comment")}}


class IngredientChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.name} · {obj.get_unit_display()}"


class PurchaseLineForm(forms.Form):
    id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    ingredient = IngredientChoiceField(label="Сырьё или упаковка", queryset=Ingredient.objects.filter(status="active"),
                                        empty_label="Выберите позицию")
    pack_size = ExactDecimalField(label="Содержимое одной упаковки", max_digits=24, decimal_places=12,
                                  min_value=Decimal("0.000000000001"),
                                  help_text="В базовой единице выбранной позиции: кг, л или шт.")
    pack_price = ExactDecimalField(label="Цена упаковки без доставки, ₸", max_digits=14, decimal_places=2, min_value=0)
    pack_count = ExactDecimalField(label="Количество упаковок", max_digits=24, decimal_places=12,
                                   min_value=Decimal("0.000000000001"), required=False,
                                   help_text="Обязательно при платной доставке и для повторных строк одной позиции.")
    comment = forms.CharField(label="Комментарий к строке (необязательно)", max_length=2000, required=False)
    is_one_off = forms.BooleanField(label="Разовая закупка", required=False)

    def __init__(self, *args, existing=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.existing = existing
        if existing is not None:
            self.fields["ingredient"].disabled = True
            self.initial["ingredient"] = existing.ingredient_id
            self.fields["pack_size"].help_text = f"В базовой единице позиции: {existing.ingredient.get_unit_display()}."

    def line_data(self):
        data = self.cleaned_data
        return {"id": data.get("id"), "ingredient_id": data["ingredient"].pk,
                **{name: data[name] for name in ("pack_size", "pack_price", "pack_count", "comment", "is_one_off")}}


class BasePurchaseLineFormSet(BaseFormSet):
    def __init__(self, *args, existing=(), **kwargs):
        self.existing = list(existing)
        super().__init__(*args, **kwargs)

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        if index is not None and index < len(self.existing):
            kwargs["existing"] = self.existing[index]
        return kwargs

    def _construct_form(self, index, **kwargs):
        kwargs["empty_permitted"] = False
        return super()._construct_form(index, **kwargs)

    def clean(self):
        if any(self.errors):
            return
        ids = [form.cleaned_data.get("id") for form in self.forms]
        expected = [row.pk for row in self.existing]
        if ids[:len(expected)] != expected or any(ids[len(expected):]):
            raise ValidationError("Состав закупки изменён некорректно. Сохранённые строки удаляются отдельной кнопкой в карточке закупки.")


PurchaseLineFormSet = formset_factory(PurchaseLineForm, formset=BasePurchaseLineFormSet, extra=0,
    min_num=1, validate_min=True, max_num=100, validate_max=True, absolute_max=100)


class PurchaseLineDeleteForm(CommandForm):
    delivery_action = forms.ChoiceField(label="Что делать с доставкой?", widget=forms.RadioSelect)
    replacement_mode = forms.ChoiceField(label="Чем заменить действующую цену", choices=[
        ("", "Выберите способ"), ("existing", "Выбрать сохранённую цену"), ("new", "Ввести новую цену")])
    replacement_price = ReplacementPriceField(label="Сохранённая цена", queryset=Price.objects.none(),
                                               required=False, empty_label="Выберите цену")

    def __init__(self, *args, last_line, replacement_ingredient=None, removed_price=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.replacement_ingredient = replacement_ingredient
        self.fields["delivery_action"].choices = ([("keep", "Сохранить сумму как нераспределённую доставку"),
            ("delete", "Удалить доставку вместе с закупкой")] if last_line else [
            ("remove_all", "Убрать доставку со всей закупки"),
            ("drop_share", "Удалить только долю этой строки, сохранить остальные доли"),
            ("reallocate", "Перераспределить всю доставку между оставшимися товарами")])
        if replacement_ingredient is None:
            self.fields.pop("replacement_mode")
            self.fields.pop("replacement_price")
        else:
            self.fields["replacement_price"].queryset = Price.objects.filter(
                ingredient=replacement_ingredient).exclude(pk=removed_price.pk).select_related("ingredient", "supplier")

    def clean(self):
        data = super().clean()
        if self.replacement_ingredient is not None and data.get("replacement_mode") == "existing" and not data.get("replacement_price"):
            self.add_error("replacement_price", "Выберите цену, которая станет действующей.")
        return data


class DeliveryForm(CommandForm):
    amount = ExactDecimalField(label="Сумма доставки, ₸", max_digits=14, decimal_places=2, min_value=0)
    note = forms.CharField(label="Заметка (необязательно)", max_length=2000, required=False,
                           widget=forms.Textarea(attrs={"rows": 3}))
    reason = forms.CharField(label="Причина исправления", max_length=2000, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, editing=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not editing:
            self.fields.pop("reason")

    def delivery_data(self):
        return {name: self.cleaned_data[name] for name in ("amount", "note")}


class PurchaseChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        supplier = obj.supplier.name if obj.supplier else "Поставщик не указан"
        names = list(dict.fromkeys(line.ingredient.name for line in obj.lines.all()))
        contents = ", ".join(names[:2])
        if len(names) > 2:
            contents += f" и ещё {len(names) - 2}"
        amount = f"{obj.delivery_total:.2f}".replace(".", ",")
        return f"{obj.purchase_date:%d.%m.%Y} · {supplier} · {contents} · доставка {amount} ₸"


class DeliveryAttachForm(CommandForm):
    purchase = PurchaseChoiceField(label="Закупка для переноса всей суммы", queryset=Purchase.objects.select_related(
                                      "supplier").prefetch_related("lines__ingredient"),
                                      empty_label="Выберите закупку")
    confirm_addition = forms.BooleanField(required=False,
        label="Подтверждаю: это дополнительная доставка, её сумма прибавляется к доставке закупки")


class DeliveryQuantityForm(forms.Form):
    id = forms.UUIDField(widget=forms.HiddenInput)
    pack_count = ExactDecimalField(label="Количество упаковок", max_digits=24, decimal_places=12,
                                   min_value=Decimal("0.000000000001"))


DeliveryQuantityFormSet = formset_factory(DeliveryQuantityForm, extra=0, max_num=100,
                                         validate_max=True, absolute_max=100)


class PurchaseListForm(forms.Form):
    q = forms.CharField(label="Поставщик, позиция или комментарий", max_length=200, required=False,
                        widget=forms.TextInput(attrs={"type": "search", "placeholder": "Найти закупку"}))
    supplier = forms.ModelChoiceField(label="Поставщик", queryset=Supplier.objects.filter(status="active"), required=False,
                                      empty_label="Все поставщики")
    sort = forms.ChoiceField(label="Порядок", required=False, choices=[
        ("newest", "Сначала новые даты"), ("oldest", "Сначала старые даты"),
        ("delivery", "Доставка: по возрастанию"), ("-delivery", "Доставка: по убыванию")])


class DeliveryListForm(forms.Form):
    q = forms.CharField(label="Заметка", max_length=200, required=False,
                        widget=forms.TextInput(attrs={"type": "search", "placeholder": "Найти доставку"}))
    sort = forms.ChoiceField(label="Порядок", required=False, choices=[
        ("newest", "Сначала новые"), ("oldest", "Сначала старые"),
        ("amount", "Сумма: по возрастанию"), ("-amount", "Сумма: по убыванию")])
