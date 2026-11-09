from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tseh", "0013_employee_access")]
    operations = [
        migrations.AddField(model_name="auditentry", name="actor_name",
            field=models.CharField("Сотрудник", max_length=150, blank=True, default="", db_default="")),
    ]
