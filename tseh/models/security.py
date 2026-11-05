"""Operational login limits; deliberately excluded from domain JSON exports."""

from django.db import models


class LoginThrottle(models.Model):
    source_key = models.CharField(primary_key=True, max_length=64, editable=False)
    window_started_at = models.DateTimeField(db_index=True)
    attempts = models.PositiveIntegerField(default=0)

