import uuid

from django.db import models
from django.db.models import Q


class CatalogDraft(models.Model):
    KINDS = [("ingredient", "Сырьё и упаковка"), ("preparation", "Заготовка"), ("dish", "Блюдо")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=12, choices=KINDS)
    name = models.CharField(max_length=160)
    payload = models.JSONField(default=dict)
    source_text = models.TextField(blank=True, default="", max_length=20000)
    notes = models.TextField(blank=True, default="", max_length=8000)
    source = models.CharField(max_length=10, choices=[("manual", "Ручной ввод"), ("import", "Импорт")], default="manual")
    version = models.PositiveIntegerField(default=1)
    published_object_id = models.UUIDField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def is_published(self):
        return self.published_at is not None

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=["ingredient", "preparation", "dish"]), name="catalog_draft_kind"),
            models.CheckConstraint(condition=Q(source__in=["manual", "import"]), name="catalog_draft_source"),
            models.CheckConstraint(condition=Q(version__gte=1), name="catalog_draft_version"),
            models.CheckConstraint(condition=(Q(published_object_id__isnull=True, published_at__isnull=True)
                | Q(published_object_id__isnull=False, published_at__isnull=False)), name="catalog_draft_publication"),
        ]
