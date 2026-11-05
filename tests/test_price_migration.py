from datetime import date
from decimal import Decimal as D
import uuid

import pytest
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_price_metadata_migration_preserves_active_price_events_and_null_supplier():
    executor = MigrationExecutor(connection)
    latest = [node for node in executor.loader.graph.leaf_nodes() if node[0] == "tseh"]
    previous = [("tseh", "0005_optional_price_supplier")]
    executor.migrate(previous)
    apps = executor.loader.project_state(previous).apps
    try:
        price_id = uuid.uuid4()
        with transaction.atomic():
            ingredient = apps.get_model("tseh", "Ingredient").objects.create(name="Существующее сырьё", unit="kg", loss=D(0), current_price_id=price_id)
            apps.get_model("tseh", "Price").objects.create(id=price_id, ingredient=ingredient, supplier=None,
                price_date=date(2026, 11, 4), pack_size=D("0.004000000001"), pack_price=D("12.34"))
        event = apps.get_model("tseh", "PriceApplication").objects.create(price_id=price_id)
        MigrationExecutor(connection).migrate(latest)
        from tseh.models import Ingredient, Price, PriceApplication
        price = Price.objects.get(pk=price_id)
        assert price.comment == "" and price.is_one_off is False
        assert price.pack_price == D("12.34") and price.pack_size == D("0.004000000001")
        assert price.supplier_id is None
        assert Ingredient.objects.get(pk=ingredient.pk).current_price_id == price_id
        restored = PriceApplication.objects.get(pk=event.pk)
        assert restored.price_id == price_id and restored.applied_at == event.applied_at
    finally:
        MigrationExecutor(connection).migrate(latest)
