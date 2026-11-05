"""Validated read-only list parameters; casefold search is independent of DB locale."""
from urllib.parse import urlencode

from django import forms
from django.core.paginator import Paginator

from tseh.models import Supplier

KINDS = [("Supplier", "Поставщики"), ("Ingredient", "Ингредиенты / упаковка"), ("Price", "Цены"),
         ("PriceApplication", "Применения цен"), ("Purchase", "Закупки"), ("PurchaseLine", "Строки закупок"), ("UnallocatedDelivery", "Доставка"), ("Dish", "Блюда"), ("Preparation", "Заготовки"), ("GlobalSettings", "Пороги"), ("DomainState", "Восстановление данных")]
UNITS = [("kg", "кг"), ("l", "л"), ("pc", "шт.")]
SORT_LABELS = {"name": "Название: А–Я", "-name": "Название: Я–А", "cost": "Себестоимость: по возрастанию",
    "-cost": "Себестоимость: по убыванию", "price": "Цена: по возрастанию", "-price": "Цена: по убыванию",
    "loss": "Потери: по возрастанию", "-loss": "Потери: по убыванию", "ratio": "Доля: по возрастанию",
    "-ratio": "Доля: по убыванию", "attention": "Требуют внимания", "newest": "Сначала новые", "oldest": "Сначала старые"}
SORTS = {"dishes": ["attention", "name", "-name", "cost", "-cost", "price", "-price", "ratio", "-ratio"],
    "ingredients": ["name", "-name", "price", "-price", "loss", "-loss"],
    "preparations": ["name", "-name", "cost", "-cost", "price", "-price"],
    "suppliers": ["name", "-name"], "journal": ["newest", "oldest"]}


def choice(label, choices):
    return forms.ChoiceField(label=label, choices=[("", "Все")] + list(choices), required=False)


class ListForm(forms.Form):
    q = forms.CharField(label="Название", max_length=160, required=False,
                        widget=forms.TextInput(attrs={"type": "search", "placeholder": "Введите часть названия"}))

    def __init__(self, data, kind):
        super().__init__(data)
        self.default_sort = SORTS[kind][0]
        if kind == "suppliers":
            self.fields["q"].label = "Название или контакты"
            self.fields["q"].widget.attrs["placeholder"] = "Найти поставщика"
        if kind == "dishes":
            self.fields["zone"] = choice("Статус", [("red", "Красная зона"), ("orange", "Оранжевая зона"),
                ("green", "Зелёная зона"), ("unconfigured", "Пороги не настроены"), ("invalid", "Некорректные пороги"), ("no_price", "Нет цены после скидки")])
        if kind == "ingredients":
            self.fields["kind"] = choice("Тип", [("ingredient", "Сырьё"), ("packaging", "Упаковка")])
            self.fields["supplier"] = choice("Поставщик", [("none", "Не указан")] + [(str(obj.pk), obj.name) for obj in Supplier.objects.filter(status="active")])
        if kind in {"ingredients", "preparations"}:
            self.fields["unit"] = choice("Единица", UNITS)
        if kind == "journal":
            self.fields["kind"] = choice("Объект", KINDS)
            for name, label in [("from_date", "С даты"), ("to_date", "По дату")]:
                self.fields[name] = forms.DateField(label=label, required=False, input_formats=["%Y-%m-%d"], widget=forms.DateInput(attrs={"type": "date"}))
        labels = dict(SORT_LABELS)
        for key, ending in [("price", "по возрастанию"), ("-price", "по убыванию")]:
            labels[key] = ("Отпускная цена: " if kind == "dishes" else "Цена единицы: ") + ending
        if kind == "preparations":
            labels.update(cost="Стоимость замеса: по возрастанию", **{"-cost": "Стоимость замеса: по убыванию"})
        self.fields["sort"] = forms.ChoiceField(label="Сортировка", required=False,
            choices=[(key, labels[key]) for key in SORTS[kind]])

    def clean(self):
        data = super().clean()
        if data.get("from_date") and data.get("to_date") and data["from_date"] > data["to_date"]:
            self.add_error("from_date", "Начальная дата должна быть не позже конечной.")
            self.add_error("to_date", "Исправьте диапазон дат.")
        return data


def parameters(request, kind):
    form = ListForm(request.GET, kind)
    form.is_valid()
    values = dict(form.cleaned_data)
    values["sort"] = values.get("sort") or form.default_sort
    return form, values


def matches(query, *texts):
    needle = (query or "").casefold()
    return not needle or any(needle in (text or "").casefold() for text in texts)


def sorted_rows(rows, key, pk, descending=False):
    stable = sorted(rows, key=pk)
    present = [row for row in stable if key(row) is not None]
    absent = [row for row in stable if key(row) is None]
    return sorted(present, key=key, reverse=descending) + absent


def list_context(request, form, values, rows):
    page = Paginator(rows, 30).get_page(request.GET.get("page"))
    params = [(key, value.isoformat() if hasattr(value, "isoformat") else str(value))
              for key, value in values.items() if value not in (None, "")]
    return {"list_form": form, "page": page, "list_total": page.paginator.count,
        "list_active": any(value not in (None, "") for key, value in values.items() if key != "sort"),
        "list_query": urlencode(params), "list_params": params, "list_reset_url": request.path}
