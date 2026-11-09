import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tseh", "0015_staff_roles_and_sessions")]
    operations = [migrations.CreateModel(name="CatalogDraft", fields=[
        ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
        ("kind", models.CharField(choices=[("ingredient", "Сырьё и упаковка"), ("preparation", "Заготовка"), ("dish", "Блюдо")], max_length=12)),
        ("name", models.CharField(max_length=160)), ("payload", models.JSONField(default=dict)),
        ("source_text", models.TextField(blank=True, default="", max_length=20000)),
        ("notes", models.TextField(blank=True, default="", max_length=8000)),
        ("source", models.CharField(choices=[("manual", "Ручной ввод"), ("import", "Импорт")], default="manual", max_length=10)),
        ("version", models.PositiveIntegerField(default=1)),
        ("published_object_id", models.UUIDField(blank=True, null=True)),
        ("published_at", models.DateTimeField(blank=True, null=True)),
        ("created_at", models.DateTimeField(auto_now_add=True)), ("updated_at", models.DateTimeField(auto_now=True)),
    ], options={"ordering": ["name", "id"], "constraints": [
        models.CheckConstraint(condition=models.Q(kind__in=["ingredient", "preparation", "dish"]), name="catalog_draft_kind"),
        models.CheckConstraint(condition=models.Q(source__in=["manual", "import"]), name="catalog_draft_source"),
        models.CheckConstraint(condition=models.Q(version__gte=1), name="catalog_draft_version"),
        models.CheckConstraint(condition=(models.Q(published_object_id__isnull=True, published_at__isnull=True)
            | models.Q(published_object_id__isnull=False, published_at__isnull=False)), name="catalog_draft_publication"),
    ]})]
