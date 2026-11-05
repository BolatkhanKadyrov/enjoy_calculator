import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_integrity_migration_preserves_previous_schema_data():
    executor = MigrationExecutor(connection)
    latest = [node for node in executor.loader.graph.leaf_nodes() if node[0] == "tseh"]
    executor.migrate([("tseh", "0001_initial")])
    old = executor.loader.project_state([("tseh", "0001_initial")]).apps
    supplier = old.get_model("tseh", "Supplier").objects.create(name="Существующий поставщик", note="Сохранить")
    try:
        executor = MigrationExecutor(connection)
        executor.migrate([("tseh", "0002_integrity_and_state")])
        new = executor.loader.project_state([("tseh", "0002_integrity_and_state")]).apps
        assert new.get_model("tseh", "Supplier").objects.get(pk=supplier.pk).note == "Сохранить"
        assert new.get_model("tseh", "DomainState").objects.filter(pk=1).count() == 1
        with connection.cursor() as cursor:
            cursor.execute("SELECT condeferrable, condeferred FROM pg_constraint WHERE conname = 'ingredient_owns_current_price'")
            assert cursor.fetchone() == (True, True)
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db(transaction=True)
def test_stage2_upgrade_preserves_recipe_price_history_and_login():
    from decimal import Decimal as D
    from datetime import date
    import uuid
    from django.contrib.auth import get_user_model
    from django.db import transaction
    executor = MigrationExecutor(connection)
    latest = [node for node in executor.loader.graph.leaf_nodes() if node[0] == "tseh"]
    executor.migrate([("tseh", "0002_integrity_and_state")])
    old = executor.loader.project_state([("tseh", "0002_integrity_and_state")]).apps
    try:
        user = get_user_model().objects.create_user(username="existing-team", password="old-password-12345")
        supplier = old.get_model("tseh", "Supplier").objects.create(name="Существующий")
        price_id = uuid.uuid4()
        with transaction.atomic():
            item = old.get_model("tseh", "Ingredient").objects.create(name="Курица", unit="kg", loss=D(25), current_price_id=price_id)
            old.get_model("tseh", "Price").objects.create(id=price_id, ingredient=item, supplier=supplier, price_date=date(2026, 11, 4), pack_size=D(5), pack_price=D(10000))
        event = old.get_model("tseh", "PriceApplication").objects.create(price_id=price_id)
        dish = old.get_model("tseh", "Dish").objects.create(name="Блюдо", base_price=D(500))
        old.get_model("tseh", "DishLine").objects.create(dish=dish, ingredient=item, quantity=D("0.004000000001"), unit="kg", position=0)
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        from tseh.models import Dish, Ingredient, Price, PriceApplication
        assert Dish.objects.get(pk=dish.pk).lines.get().quantity == D("0.004000000001")
        assert Dish.objects.get(pk=dish.pk).green_override is None
        assert Ingredient.objects.get(pk=item.pk).current_price_id == price_id
        assert Ingredient.objects.get(pk=item.pk).kind == "ingredient"
        assert PriceApplication.objects.get(pk=event.pk).price_id == price_id
        assert Price.objects.get(pk=price_id).supplier_id == supplier.pk
        assert Price._meta.get_field("supplier").null
        assert get_user_model().objects.get(pk=user.pk).check_password("old-password-12345")
    finally:
        MigrationExecutor(connection).migrate(latest)
