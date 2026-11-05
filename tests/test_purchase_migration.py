from datetime import date
from decimal import Decimal as D
import uuid

import pytest
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_purchase_upgrade_preserves_existing_prices_events_and_optional_suppliers():
    executor = MigrationExecutor(connection)
    latest = [node for node in executor.loader.graph.leaf_nodes() if node[0] == "tseh"]
    previous = [("tseh", "0006_price_comment_is_one_off")]
    executor.migrate(previous)
    apps = executor.loader.project_state(previous).apps
    try:
        price_id = uuid.uuid4()
        with transaction.atomic():
            ingredient = apps.get_model("tseh", "Ingredient").objects.create(name="Ранее сохранённое сырьё", unit="kg", loss=D(25), current_price_id=price_id)
            apps.get_model("tseh", "Price").objects.create(id=price_id, ingredient=ingredient,
                supplier=None, price_date=date(2026, 11, 4), pack_size=D("0.004000000001"),
                pack_price=D("15.27"), comment="Сохранить исходные данные", is_one_off=True)
        event = apps.get_model("tseh", "PriceApplication").objects.create(price_id=price_id)
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        from tseh.models import Ingredient, Price, PriceApplication, Purchase, PurchaseLine, UnallocatedDelivery
        price = Price.objects.get(pk=price_id)
        assert price.purchase_id is None and price.delivery_total == 0
        assert price.goods_total is None and price.quantity_total is None
        assert price.pack_price == D("15.27") and price.pack_size == D("0.004000000001")
        assert price.comment == "Сохранить исходные данные" and price.is_one_off and price.supplier_id is None
        assert Ingredient.objects.get(pk=ingredient.pk).current_price_id == price_id
        assert PriceApplication.objects.get(pk=event.pk).applied_at == event.applied_at
        assert not Purchase.objects.exists() and not PurchaseLine.objects.exists() and not UnallocatedDelivery.objects.exists()
    finally:
        MigrationExecutor(connection).migrate(latest)
