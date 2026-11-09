from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def seed_owners(apps, schema_editor):
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(app_label, model_name)
    Access = apps.get_model("tseh", "AccountAccess")
    for user in User.objects.filter(username__in=["tseh", "tseh_staging"], is_active=True):
        Access.objects.get_or_create(user_id=user.pk, defaults={"is_owner": True})


class Migration(migrations.Migration):
    dependencies = [("tseh", "0012_login_throttle"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [
        migrations.CreateModel(name="AccountAccess", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("is_owner", models.BooleanField(default=False)),
            ("permissions", models.JSONField(default=list)),
            ("version", models.PositiveIntegerField(default=1)),
            ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                                          related_name="account_access", to=settings.AUTH_USER_MODEL)),
        ], options={"constraints": [models.CheckConstraint(condition=models.Q(version__gte=1),
                                                             name="account_access_positive_version")]}),
        migrations.CreateModel(name="AccountEvent", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("at", models.DateTimeField(auto_now_add=True)),
            ("actor_name", models.CharField(max_length=150)),
            ("target_name", models.CharField(max_length=150)),
            ("action", models.CharField(choices=[("create", "Добавлен сотрудник"), ("update", "Изменён доступ"),
                                                ("disable", "Отключён сотрудник")], max_length=12)),
            ("before", models.JSONField(null=True)), ("after", models.JSONField(null=True)),
        ], options={"ordering": ["-at", "-id"], "constraints": [models.CheckConstraint(
            condition=models.Q(action__in=["create", "update", "disable"]), name="account_event_valid_action")]}),
        migrations.RunPython(seed_owners, migrations.RunPython.noop),
    ]
