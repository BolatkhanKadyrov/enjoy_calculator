from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tseh", "0005_optional_price_supplier")]
    operations = [
        migrations.AddField(model_name="price", name="comment",
            field=models.TextField("Комментарий", blank=True, default="", db_default="", max_length=2000)),
        migrations.AddField(model_name="price", name="is_one_off",
            field=models.BooleanField("Разовая закупка", default=False, db_default=False)),
    ]
