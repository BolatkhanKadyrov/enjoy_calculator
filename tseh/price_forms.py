from decimal import Decimal

from django import forms
from django.utils import timezone
from django.utils.html import strip_tags
from tseh.templatetags.display import cash

from tseh.forms import CommandForm, ExactDecimalField
from tseh.models import Price, Supplier


class PriceDataForm(forms.Form):
    supplier = forms.ModelChoiceField(label="Поставщик (необязательно)", queryset=Supplier.objects.filter(status="active"),
                                     required=False, empty_label="Не указан")
    price_date = forms.DateField(label="Дата цены", initial=timezone.localdate,
                                widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
                                input_formats=["%Y-%m-%d"])
    pack_size = ExactDecimalField(label="Содержимое одной упаковки", max_digits=24, decimal_places=12,
                                  min_value=Decimal("0.000000000001"))
    pack_price = ExactDecimalField(label="Цена одной упаковки, ₸", max_digits=14, decimal_places=2, min_value=0)
    comment = forms.CharField(label="Комментарий (необязательно)", max_length=2000, required=False,
                             widget=forms.Textarea(attrs={"rows": 3}))
    is_one_off = forms.BooleanField(label="Разовая закупка", required=False,
                                   help_text="После применения действует до следующей явной смены цены.")
    confirm_zero = forms.BooleanField(label="Подтверждаю нулевую закупочную цену", required=False,
                                      help_text="Нулевая действующая цена снизит себестоимость.")

    def __init__(self, *args, ingredient=None, **kwargs):
        super().__init__(*args, **kwargs)
        if ingredient is not None:
            self.fields["pack_size"].help_text = f"В базовой единице этой позиции: {ingredient.get_unit_display()}."

    def clean(self):
        data = super().clean()
        if data.get("pack_price") == 0 and not data.get("confirm_zero"):
            self.add_error("pack_price", "Нулевая цена снизит себестоимость. Подтвердите её отдельной отметкой.")
        return data

    def price_data(self):
        """Call after successful validation; confirmation and command tokens stay separate."""
        data = self.cleaned_data
        return {"supplier_id": data["supplier"].pk if data["supplier"] else None,
                **{name: data[name] for name in ("price_date", "pack_size", "pack_price", "comment", "is_one_off")}}


class PriceForm(CommandForm, PriceDataForm):
    def __init__(self, *args, include_apply=False, include_reason=False, **kwargs):
        super().__init__(*args, **kwargs)
        if include_apply:
            self.fields["apply_now"] = forms.BooleanField(label="Сразу сделать действующей", required=False,
                help_text="Без этой отметки цена сохранится как предложение и не изменит расчёты.")
        if include_reason:
            self.fields["reason"] = forms.CharField(label="Причина исправления (необязательно)", max_length=2000,
                required=False, widget=forms.Textarea(attrs={"rows": 2}), help_text="Будет записана в журнал действий.")


class ReplacementPriceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        supplier = obj.supplier.name if obj.supplier else "Поставщик не указан"
        if obj.purchase_id:
            return f"{obj.price_date:%d.%m.%Y} · {supplier} · {strip_tags(str(cash(obj.effective_unit_price)))} ₸/{obj.ingredient.get_unit_display()} с доставкой · из закупки"
        return f"{obj.price_date:%d.%m.%Y} · {supplier} · {obj.pack_price} ₸ / {obj.pack_size} {obj.ingredient.get_unit_display()}"


class PriceDeleteForm(CommandForm):
    replacement_mode = forms.ChoiceField(label="Чем заменить действующую цену", required=False,
        choices=[("", "Выберите способ"), ("existing", "Выбрать сохранённую цену"), ("new", "Ввести новую цену")])
    replacement_price = ReplacementPriceField(label="Сохранённая цена", queryset=Price.objects.none(),
                                              required=False, empty_label="Выберите цену")

    def __init__(self, *args, ingredient, price, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_current = ingredient.current_price_id == price.pk
        if self.is_current:
            self.fields["replacement_mode"].required = True
            self.fields["replacement_price"].queryset = Price.objects.filter(ingredient=ingredient).exclude(pk=price.pk).select_related("supplier", "ingredient").order_by("-price_date", "-created_at", "id")
        else:
            self.fields.pop("replacement_mode")
            self.fields.pop("replacement_price")

    def clean(self):
        data = super().clean()
        if self.is_current and data.get("replacement_mode") == "existing" and not data.get("replacement_price"):
            self.add_error("replacement_price", "Выберите цену, которая станет действующей.")
        if data.get("replacement_mode") == "new":
            data["replacement_price"] = None
        return data


class PriceApplyForm(CommandForm):
    pass


class PriceListForm(forms.Form):
    q = forms.CharField(label="Поставщик или комментарий", max_length=200, required=False,
                        widget=forms.TextInput(attrs={"type": "search", "placeholder": "Поиск по сохранённым ценам"}))
    status = forms.ChoiceField(label="Какие цены показать", required=False, choices=[
        ("", "Все цены"), ("current", "Действующая"), ("unapplied", "Ни разу не применялись")])
    sort = forms.ChoiceField(label="Порядок", required=False, choices=[
        ("date_desc", "Сначала новые даты цен"), ("date_asc", "Сначала старые даты цен"),
        ("price", "Сначала дешевле за базовую единицу"), ("-price", "Сначала дороже за базовую единицу")], initial="date_desc")
