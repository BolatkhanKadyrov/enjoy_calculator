import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("tseh", "0009_dish_lifecycle"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.CreateModel(
        name="RestoreSession",
        fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("proposed", models.JSONField()),
            ("previous", models.JSONField()),
            ("source_hash", models.CharField(max_length=64)),
            ("baseline_epoch", models.UUIDField()),
            ("baseline_revision", models.PositiveBigIntegerField()),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("expires_at", models.DateTimeField(db_index=True)),
            ("downloaded_at", models.DateTimeField(null=True)),
            ("completed_at", models.DateTimeField(null=True)),
            ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
        ],
    )]

