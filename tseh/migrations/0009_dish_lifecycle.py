from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tseh", "0008_audit_scenario_source")]

    operations = [
        migrations.AddField(model_name="dish", name="status", field=models.CharField(
            "Состояние", max_length=8, choices=[("active", "В ассортименте"), ("archived", "В архиве"),
            ("trashed", "В корзине")], default="active", db_default="active")),
        migrations.AddField(model_name="dish", name="archived_at", field=models.DateTimeField(
            "Последнее помещение в архив", null=True, blank=True)),
        migrations.AddField(model_name="dish", name="trashed_at", field=models.DateTimeField(
            "Последнее помещение в корзину", null=True, blank=True)),
        migrations.AddField(model_name="dish", name="purge_at", field=models.DateTimeField(
            "Срок окончательного удаления", null=True, blank=True, db_index=True)),
        migrations.AddConstraint(model_name="dish", constraint=models.CheckConstraint(
            condition=models.Q(status__in=["active", "archived", "trashed"]), name="dish_valid_status")),
        migrations.AddConstraint(model_name="dish", constraint=models.CheckConstraint(
            condition=(models.Q(status="active", purge_at__isnull=True)
                | models.Q(status="archived", archived_at__isnull=False, purge_at__isnull=True)
                | models.Q(status="trashed", archived_at__isnull=False, trashed_at__isnull=False, purge_at__isnull=False)),
            name="dish_lifecycle_dates")),
        migrations.AddConstraint(model_name="dish", constraint=models.CheckConstraint(
            condition=models.Q(trashed_at__isnull=True) | models.Q(archived_at__isnull=False),
            name="dish_trash_has_archive")),
    ]
