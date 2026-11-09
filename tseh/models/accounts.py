"""Staff access and its audit stay outside transferable business snapshots."""
from django.conf import settings
from django.db import models
from django.db.models import Q


class AccountAccess(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="account_access")
    is_owner = models.BooleanField(default=False)
    permissions = models.JSONField(default=list)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(version__gte=1), name="account_access_positive_version")]


class AccountEvent(models.Model):
    ACTIONS = [("create", "Добавлен сотрудник"), ("update", "Изменён доступ"),
               ("disable", "Отключён сотрудник")]
    at = models.DateTimeField(auto_now_add=True)
    actor_name = models.CharField(max_length=150)
    target_name = models.CharField(max_length=150)
    action = models.CharField(max_length=12, choices=ACTIONS)
    before = models.JSONField(null=True)
    after = models.JSONField(null=True)

    class Meta:
        ordering = ["-at", "-id"]
        constraints = [models.CheckConstraint(condition=Q(action__in=["create", "update", "disable"]),
                                               name="account_event_valid_action")]
