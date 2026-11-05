"""Explicit release command; importing this module never touches the database.

The current PostgreSQL migrations are atomic. Non-atomic future migrations must
use a separately reviewed maintenance procedure, not this release wrapper.
"""
import os
from pathlib import Path
import sys

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor

MIGRATION_LOCK = 854328761302  # Application-specific PostgreSQL advisory lock.


def deploy_database():
    """Migrate and invalidate old forms on the same guarded DB connection."""
    from tseh.models import DomainState

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [MIGRATION_LOCK])
            if not cursor.fetchone()[0]:
                raise CommandError("Another release is migrating this database. Retry after it finishes.")
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if any(not migration.atomic for migration, _ in plan):
            raise CommandError("A non-atomic migration requires a separate maintenance procedure; nothing was migrated.")
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '15s'")
                cursor.execute("SET LOCAL statement_timeout = '180s'")
                cursor.execute("SELECT to_regclass(%s)", [DomainState._meta.db_table])
                existing_state = cursor.fetchone()[0] is not None
            if existing_state:
                # Web commands and consistent read snapshots use this same row.
                DomainState.objects.select_for_update().get(pk=1)
            call_command("migrate", interactive=False, verbosity=1)
            state = DomainState.objects.select_for_update().get(pk=1)
            state.revision += 1
            state.save(update_fields=["revision"])
        print(f"Release database preparation complete; migrations planned: {len(plan)}. Old forms invalidated.", flush=True)
    finally:
        # A session advisory lock is released even after exceptions or timeouts.
        connection.close()


def main():
    project = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    deploy_database()


if __name__ == "__main__":
    main()
