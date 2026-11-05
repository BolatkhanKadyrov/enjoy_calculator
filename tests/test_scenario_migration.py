import uuid

import pytest
from django.db import connection, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_audit_source_upgrade_preserves_old_journal_and_validates_new_sources():
    executor = MigrationExecutor(connection)
    latest = [node for node in executor.loader.graph.leaf_nodes() if node[0] == "tseh"]
    previous = [("tseh", "0007_purchases_and_delivery")]
    executor.migrate(previous)
    old = executor.loader.project_state(previous).apps
    try:
        group = uuid.uuid4()
        entry = old.get_model("tseh", "AuditEntry").objects.create(group=group, object_type="Price",
            object_id=str(uuid.uuid4()), object_name="Историческая цена", action="update",
            before={"pack_price": "123.45"}, after={"pack_price": "234.56"})
        MigrationExecutor(connection).migrate(latest)
        from tseh.models import AuditEntry
        restored = AuditEntry.objects.get(pk=entry.pk)
        assert restored.at == entry.at and restored.group == group and restored.source == "manual"
        assert restored.before == {"pack_price": "123.45"} and restored.after == {"pack_price": "234.56"}
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                AuditEntry.objects.filter(pk=entry.pk).update(source="unsupported")
        AuditEntry.objects.filter(pk=entry.pk).update(source="scenario")
        assert AuditEntry.objects.get(pk=entry.pk).source == "scenario"
    finally:
        MigrationExecutor(connection).migrate(latest)
