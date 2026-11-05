"""Short-lived restore staging, excluded from domain backup files."""
import uuid

from django.conf import settings
from django.db import models


class RestoreSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    proposed = models.JSONField()
    previous = models.JSONField()
    source_hash = models.CharField(max_length=64)
    baseline_epoch = models.UUIDField()
    baseline_revision = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    downloaded_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)

