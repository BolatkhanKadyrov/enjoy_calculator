from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tseh", "0010_restore_session")]

    operations = [operation for model_name in ("supplier", "ingredient", "preparation") for operation in (
        migrations.AddField(model_name=model_name, name="status", field=models.CharField(
            "Состояние", max_length=8, choices=[("active", "В справочнике"), ("trashed", "В корзине")],
            default="active", db_default="active")),
        migrations.AddField(model_name=model_name, name="trashed_at", field=models.DateTimeField(
            "Последнее помещение в корзину", null=True, blank=True)),
        migrations.AddConstraint(model_name=model_name, constraint=models.CheckConstraint(
            condition=models.Q(status__in=["active", "trashed"]), name=f"{model_name}_valid_status")),
        migrations.AddConstraint(model_name=model_name, constraint=models.CheckConstraint(
            condition=models.Q(status="active") | models.Q(trashed_at__isnull=False), name=f"{model_name}_trash_date")),
    )]
