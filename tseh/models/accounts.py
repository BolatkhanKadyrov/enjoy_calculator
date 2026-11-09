"""Staff access and its audit stay outside transferable business snapshots."""
from django.conf import settings
from django.db import models
from django.db.models import Q


class AccountAccess(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Владелец"
        ADMIN = "admin", "Администратор"
        EMPLOYEE = "employee", "Сотрудник"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="account_access")
    is_owner = models.BooleanField(default=False)
    is_admin = models.BooleanField(default=False, db_default=False)
    permissions = models.JSONField(default=list)
    version = models.PositiveIntegerField(default=1)

    @property
    def role(self):
        return self.Role.OWNER if self.is_owner else self.Role.ADMIN if self.is_admin else self.Role.EMPLOYEE

    @property
    def role_label(self):
        return self.Role(self.role).label

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(version__gte=1), name="account_access_positive_version"),
                       models.CheckConstraint(condition=~Q(is_owner=True, is_admin=True),
                                              name="account_access_single_role")]


class AccountEvent(models.Model):
    ACTIONS = [("create", "Добавлен сотрудник"), ("update", "Изменён доступ"),
               ("disable", "Отключён сотрудник"), ("reset_sessions", "Завершены сеансы")]
    at = models.DateTimeField(auto_now_add=True)
    actor_name = models.CharField(max_length=150)
    target_name = models.CharField(max_length=150)
    action = models.CharField(max_length=20, choices=ACTIONS)
    before = models.JSONField(null=True)
    after = models.JSONField(null=True)

    class Meta:
        ordering = ["-at", "-id"]
        constraints = [models.CheckConstraint(condition=Q(action__in=["create", "update", "disable", "reset_sessions"]),
                                               name="account_event_valid_action")]
