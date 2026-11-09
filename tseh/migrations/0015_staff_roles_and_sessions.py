from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tseh", "0014_audit_actor")]
    operations = [
        migrations.AddField(model_name="accountaccess", name="is_admin",
            field=models.BooleanField(default=False, db_default=False)),
        migrations.AddConstraint(model_name="accountaccess",
            constraint=models.CheckConstraint(condition=~models.Q(is_owner=True, is_admin=True),
                                              name="account_access_single_role")),
        migrations.AlterField(model_name="accountevent", name="action",
            field=models.CharField(choices=[("create", "Добавлен сотрудник"), ("update", "Изменён доступ"),
                ("disable", "Отключён сотрудник"), ("reset_sessions", "Завершены сеансы")], max_length=20)),
        migrations.RemoveConstraint(model_name="accountevent", name="account_event_valid_action"),
        migrations.AddConstraint(model_name="accountevent", constraint=models.CheckConstraint(
            condition=models.Q(action__in=["create", "update", "disable", "reset_sessions"]),
            name="account_event_valid_action")),
    ]
