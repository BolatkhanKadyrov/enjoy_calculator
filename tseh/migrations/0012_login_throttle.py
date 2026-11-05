from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tseh", "0011_catalog_lifecycle")]

    operations = [
        migrations.CreateModel(
            name="LoginThrottle",
            fields=[
                ("source_key", models.CharField(editable=False, max_length=64, primary_key=True, serialize=False)),
                ("window_started_at", models.DateTimeField(db_index=True)),
                ("attempts", models.PositiveIntegerField(default=0)),
            ],
        ),
    ]

