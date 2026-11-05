import uuid
from decimal import Decimal

from django.db import models
from django.db.models import Q

from .transfer import RestoreSession
from .security import LoginThrottle

CATALOG_STATUSES = [("active", "В справочнике"), ("trashed", "В корзине")]


class Supplier(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("Название", max_length=160)
    note = models.TextField("Контакты / заметка", blank=True, max_length=2000)
    status = models.CharField("Состояние", max_length=8, choices=CATALOG_STATUSES, default="active", db_default="active")
    trashed_at = models.DateTimeField("Последнее помещение в корзину", null=True, blank=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=["active", "trashed"]), name="supplier_valid_status"),
            models.CheckConstraint(condition=Q(status="active") | Q(trashed_at__isnull=False), name="supplier_trash_date"),
        ]

    def __str__(self):
        return self.name


class Ingredient(models.Model):
    UNITS = [("kg", "кг"), ("l", "л"), ("pc", "шт.")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("Название", max_length=160)
    kind = models.CharField("Тип", max_length=12, choices=[("ingredient", "Ингредиент"), ("packaging", "Упаковка")], default="ingredient", db_default="ingredient")
    unit = models.CharField("Базовая единица", max_length=2, choices=UNITS)
    loss = models.DecimalField("Первичные потери, %", max_digits=11, decimal_places=8, default=Decimal(0))
    current_price = models.ForeignKey("Price", on_delete=models.PROTECT, related_name="selected_by")
    status = models.CharField("Состояние", max_length=8, choices=CATALOG_STATUSES, default="active", db_default="active")
    trashed_at = models.DateTimeField("Последнее помещение в корзину", null=True, blank=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(loss__gte=0, loss__lt=100), name="ingredient_loss_range"),
            models.CheckConstraint(condition=Q(unit__in=["kg", "l", "pc"]), name="ingredient_valid_unit"),
            models.CheckConstraint(condition=Q(kind__in=["ingredient", "packaging"]), name="ingredient_valid_kind"),
            models.CheckConstraint(condition=Q(kind="ingredient") | Q(loss=0), name="packaging_zero_loss"),
            models.CheckConstraint(condition=Q(status__in=["active", "trashed"]), name="ingredient_valid_status"),
            models.CheckConstraint(condition=Q(status="active") | Q(trashed_at__isnull=False), name="ingredient_trash_date"),
        ]

    def __str__(self):
        return self.name


class Price(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ingredient = models.ForeignKey(Ingredient, on_delete=models.PROTECT, related_name="prices")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True)
    price_date = models.DateField("Дата цены")
    created_at = models.DateTimeField(auto_now_add=True)
    pack_size = models.DecimalField("Содержимое упаковки", max_digits=24, decimal_places=12, null=True, blank=True)
    pack_price = models.DecimalField("Цена упаковки, ₸", max_digits=14, decimal_places=2, null=True, blank=True)
    purchase = models.ForeignKey("Purchase", on_delete=models.PROTECT, null=True, blank=True, related_name="prices")
    goods_total = models.DecimalField(max_digits=56, decimal_places=24, null=True, blank=True)
    quantity_total = models.DecimalField(max_digits=56, decimal_places=24, null=True, blank=True)
    delivery_total = models.DecimalField(max_digits=16, decimal_places=2, default=Decimal(0))
    comment = models.TextField("Комментарий", blank=True, default="", db_default="", max_length=2000)
    is_one_off = models.BooleanField("Разовая закупка", default=False, db_default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(pack_size__gt=0), name="price_positive_pack"),
            models.CheckConstraint(condition=Q(pack_price__gte=0), name="price_nonnegative"),
            models.UniqueConstraint(fields=["ingredient", "id"], name="price_owner_unique"),
            models.UniqueConstraint(fields=["purchase", "ingredient"], name="purchase_ingredient_price_unique"),
            models.CheckConstraint(condition=(Q(purchase__isnull=True, pack_size__isnull=False, pack_price__isnull=False, goods_total__isnull=True, quantity_total__isnull=True, delivery_total=0) | Q(purchase__isnull=False, goods_total__isnull=False, goods_total__gte=0, quantity_total__isnull=False, quantity_total__gt=0, delivery_total__gte=0)), name="price_source_consistent"),
        ]

    @property
    def effective_unit_price(self):
        from fractions import Fraction
        from tseh.calculation import unit_price
        if self.purchase_id:
            return (Fraction(self.goods_total) + Fraction(self.delivery_total)) / Fraction(self.quantity_total)
        return unit_price(self.pack_price, self.pack_size)


class Purchase(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True)
    purchase_date = models.DateField("Дата закупки")
    created_at = models.DateTimeField(auto_now_add=True)
    delivery_total = models.DecimalField("Доставка, ₸", max_digits=14, decimal_places=2, default=Decimal(0))
    comment = models.TextField("Комментарий", blank=True, default="", max_length=2000)

    class Meta:
        ordering = ["-purchase_date", "-created_at", "id"]
        constraints = [models.CheckConstraint(condition=Q(delivery_total__gte=0), name="purchase_delivery_nonnegative")]

    def __str__(self):
        return f"Закупка от {self.purchase_date}"


class PurchaseLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="lines")
    ingredient = models.ForeignKey(Ingredient, on_delete=models.PROTECT, related_name="purchase_lines")
    position = models.PositiveIntegerField()
    pack_size = models.DecimalField("Фасовка", max_digits=24, decimal_places=12)
    pack_price = models.DecimalField("Цена упаковки, ₸", max_digits=14, decimal_places=2)
    pack_count = models.DecimalField("Количество упаковок", max_digits=24, decimal_places=12, null=True, blank=True)
    delivery_share = models.DecimalField("Доля доставки, ₸", max_digits=14, decimal_places=2, default=Decimal(0))
    comment = models.TextField(blank=True, default="", max_length=2000)
    is_one_off = models.BooleanField(default=False)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["purchase", "position"], name="purchase_line_position_unique"),
            models.CheckConstraint(condition=Q(pack_size__gt=0), name="purchase_line_pack_positive"),
            models.CheckConstraint(condition=Q(pack_price__gte=0), name="purchase_line_price_nonnegative"),
            models.CheckConstraint(condition=Q(pack_count__isnull=True) | Q(pack_count__gt=0), name="purchase_line_count_positive"),
            models.CheckConstraint(condition=Q(delivery_share__gte=0), name="purchase_line_delivery_nonnegative"),
        ]

    @property
    def goods_total(self):
        from fractions import Fraction
        return None if self.pack_count is None else Fraction(self.pack_count) * Fraction(self.pack_price)

    @property
    def quantity_total(self):
        from fractions import Fraction
        return None if self.pack_count is None else Fraction(self.pack_count) * Fraction(self.pack_size)

    @property
    def effective_unit_price(self):
        from fractions import Fraction
        from tseh.calculation import unit_price
        if self.pack_count is None:
            return unit_price(self.pack_price, self.pack_size)
        return (self.goods_total + Fraction(self.delivery_share)) / self.quantity_total


class UnallocatedDelivery(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    amount = models.DecimalField("Сумма доставки, ₸", max_digits=14, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    note = models.TextField("Заметка", blank=True, default="", max_length=2000)

    class Meta:
        ordering = ["-created_at", "id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gte=0), name="unallocated_delivery_nonnegative")]

    def __str__(self):
        return f"Нераспределённая доставка {self.amount} ₸"


class PriceApplication(models.Model):
    id = models.BigAutoField(primary_key=True)
    price = models.ForeignKey(Price, on_delete=models.PROTECT, related_name="applications")
    applied_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["applied_at", "id"]


class Dish(models.Model):
    STATUSES = [("active", "В ассортименте"), ("archived", "В архиве"), ("trashed", "В корзине")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("Название блюда", max_length=160)
    base_price = models.DecimalField("Отпускная цена за порцию, ₸", max_digits=14, decimal_places=2)
    green_override = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    orange_override = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    status = models.CharField("Состояние", max_length=8, choices=STATUSES, default="active", db_default="active")
    archived_at = models.DateTimeField("Последнее помещение в архив", null=True, blank=True)
    trashed_at = models.DateTimeField("Последнее помещение в корзину", null=True, blank=True)
    purge_at = models.DateTimeField("Срок окончательного удаления", null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [models.CheckConstraint(condition=Q(base_price__gte=0), name="dish_price_nonnegative"),
            models.CheckConstraint(condition=Q(green_override__isnull=True) | Q(green_override__gt=0), name="dish_green_positive"),
            models.CheckConstraint(condition=Q(orange_override__isnull=True) | Q(orange_override__gt=0), name="dish_orange_positive"),
            models.CheckConstraint(condition=Q(status__in=["active", "archived", "trashed"]), name="dish_valid_status"),
            models.CheckConstraint(condition=(Q(status="active", purge_at__isnull=True)
                | Q(status="archived", archived_at__isnull=False, purge_at__isnull=True)
                | Q(status="trashed", archived_at__isnull=False, trashed_at__isnull=False, purge_at__isnull=False)),
                name="dish_lifecycle_dates"),
            models.CheckConstraint(condition=Q(trashed_at__isnull=True) | Q(archived_at__isnull=False),
                name="dish_trash_has_archive")]

    def __str__(self):
        return self.name


class DishLine(models.Model):
    UNITS = [("g", "г"), ("kg", "кг"), ("ml", "мл"), ("l", "л"), ("pc", "шт.")]
    dish = models.ForeignKey(Dish, on_delete=models.CASCADE, related_name="lines")
    ingredient = models.ForeignKey(Ingredient, on_delete=models.PROTECT, null=True, blank=True)
    preparation = models.ForeignKey("Preparation", on_delete=models.PROTECT, null=True, blank=True, related_name="dish_uses")
    quantity = models.DecimalField("Количество компонента", max_digits=30, decimal_places=15)
    unit = models.CharField("Единица", max_length=2, choices=UNITS)
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="line_positive_quantity"),
            models.CheckConstraint(condition=Q(unit__in=["g", "kg", "ml", "l", "pc"]), name="line_valid_unit"),
            models.UniqueConstraint(fields=["dish", "position"], name="line_position_unique"),
            models.CheckConstraint(condition=(Q(ingredient__isnull=False, preparation__isnull=True) | Q(ingredient__isnull=True, preparation__isnull=False)), name="dish_line_one_component"),
        ]

    @property
    def component(self):
        return self.ingredient if self.ingredient_id else self.preparation


class Preparation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("Название заготовки", max_length=160)
    output_quantity = models.DecimalField("Выход после приготовления", max_digits=30, decimal_places=15)
    output_unit = models.CharField("Единица выхода", max_length=2, choices=DishLine.UNITS)
    status = models.CharField("Состояние", max_length=8, choices=CATALOG_STATUSES, default="active", db_default="active")
    trashed_at = models.DateTimeField("Последнее помещение в корзину", null=True, blank=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [models.CheckConstraint(condition=Q(output_quantity__gt=0), name="preparation_positive_output"),
            models.CheckConstraint(condition=Q(output_unit__in=["g", "kg", "ml", "l", "pc"]), name="preparation_valid_unit"),
            models.CheckConstraint(condition=Q(status__in=["active", "trashed"]), name="preparation_valid_status"),
            models.CheckConstraint(condition=Q(status="active") | Q(trashed_at__isnull=False), name="preparation_trash_date")]

    @property
    def unit(self):
        from tseh.calculation import FACTORS
        return FACTORS[self.output_unit][0]

    def get_unit_display(self):
        return dict(Ingredient.UNITS)[self.unit]

    def __str__(self):
        return self.name


class PreparationLine(models.Model):
    parent = models.ForeignKey(Preparation, on_delete=models.CASCADE, related_name="lines")
    ingredient = models.ForeignKey(Ingredient, on_delete=models.PROTECT, null=True, blank=True)
    preparation = models.ForeignKey(Preparation, on_delete=models.PROTECT, null=True, blank=True, related_name="preparation_uses")
    quantity = models.DecimalField(max_digits=30, decimal_places=15)
    unit = models.CharField(max_length=2, choices=DishLine.UNITS)
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position", "id"]
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="prep_line_positive_quantity"),
            models.CheckConstraint(condition=Q(unit__in=["g", "kg", "ml", "l", "pc"]), name="prep_line_valid_unit"),
            models.CheckConstraint(condition=(Q(ingredient__isnull=False, preparation__isnull=True) | Q(ingredient__isnull=True, preparation__isnull=False)), name="prep_line_one_component"),
            models.CheckConstraint(condition=~Q(parent=models.F("preparation")), name="prep_line_not_self"),
            models.UniqueConstraint(fields=["parent", "position"], name="prep_line_position_unique")]

    @property
    def component(self):
        return self.ingredient if self.ingredient_id else self.preparation


SETTINGS_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class GlobalSettings(models.Model):
    id = models.UUIDField(primary_key=True, default=SETTINGS_ID, editable=False)
    green = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    orange = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(id=SETTINGS_ID), name="singleton_global_settings"),
            models.CheckConstraint(condition=Q(green__isnull=True) | Q(green__gt=0), name="global_green_positive"),
            models.CheckConstraint(condition=Q(orange__isnull=True) | Q(orange__gt=0), name="global_orange_positive")]

    def __str__(self):
        return "Общие пороги зон"


class DomainState(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    revision = models.PositiveBigIntegerField(default=0)
    epoch = models.UUIDField(default=uuid.uuid4)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(id=1), name="singleton_domain_state")]


class Operation(models.Model):
    id = models.UUIDField(primary_key=True)
    epoch = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    result = models.UUIDField()
    created_at = models.DateTimeField(auto_now_add=True)


class AuditEntry(models.Model):
    SOURCES = [("manual", "Ручной ввод"), ("scenario", "Применение сценария"),
               ("import", "Импорт"), ("cleanup", "Очистка")]
    id = models.BigAutoField(primary_key=True)
    at = models.DateTimeField(auto_now_add=True)
    group = models.UUIDField()
    object_type = models.CharField(max_length=40)
    object_id = models.CharField(max_length=80)
    object_name = models.CharField(max_length=160)
    action = models.CharField(max_length=40)
    before = models.JSONField(null=True)
    after = models.JSONField(null=True)
    source = models.CharField(max_length=30, default="manual", choices=SOURCES)

    class Meta:
        ordering = ["-at", "-id"]
        constraints = [models.CheckConstraint(condition=Q(source__in=["manual", "scenario", "import", "cleanup"]), name="audit_valid_source")]
