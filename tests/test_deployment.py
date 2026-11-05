"""Release safeguards and launcher checks; Linux container smoke is separate."""
from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from django.core.management.base import CommandError

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "start-production.sh"


def shell_path():
    candidates = [shutil.which("sh"), r"C:\Program Files\Git\usr\bin\sh.exe"]
    return next((str(candidate) for candidate in candidates if candidate and Path(candidate).is_file()), None)


@pytest.fixture
def launcher(tmp_path):
    shell = shell_path()
    if not shell:
        pytest.skip("POSIX shell is unavailable; run launcher checks in the Linux image.")
    binary = tmp_path / "bin"
    binary.mkdir()
    command = binary / "gunicorn"
    command.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CALL_LOG"\nexit "${FAKE_EXIT_CODE:-0}"\n', encoding="utf-8")
    command.chmod(0o755)
    python = binary / "python"
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$2" >> "$CALL_LOG"\n'
                      'if [ "$2" = "${FAIL_COMMAND:-}" ]; then exit 9; fi\n', encoding="utf-8")
    python.chmod(0o755)
    log = tmp_path / "calls.txt"
    def run(*args, **variables):
        env = {key: value for key, value in os.environ.items()
               if key not in {"PORT", "WEB_CONCURRENCY", "GUNICORN_THREADS", "GUNICORN_TIMEOUT"}}
        env.update(PATH=str(binary) + os.pathsep + env.get("PATH", ""), CALL_LOG=log.as_posix(), **variables)
        result = subprocess.run([shell, SCRIPT.as_posix(), *args], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=15)
        return result, log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return run


def test_web_launcher_passes_bound_port_and_no_migration_to_gunicorn(launcher):
    result, arguments = launcher(PORT="9876", WEB_CONCURRENCY="3", GUNICORN_THREADS="4", GUNICORN_TIMEOUT="90")
    assert result.returncode == 0, result.stderr
    assert arguments[0] == "config.wsgi:application"
    assert arguments[arguments.index("--bind") + 1] == "0.0.0.0:9876"
    assert arguments[arguments.index("--workers") + 1] == "3"
    assert arguments[arguments.index("--threads") + 1] == "4"
    assert arguments[arguments.index("--timeout") + 1] == "90"
    assert arguments[arguments.index("--forwarded-allow-ips") + 1] == ""
    assert "migrate" not in arguments and "createsuperuser" not in arguments
    assert "%(q)s" not in arguments[arguments.index("--access-logformat") + 1]


@pytest.mark.parametrize("variables", [{"PORT": "0"}, {"PORT": "65536"}, {"PORT": "8000 --help"},
    {"PORT": "$(touch unexpected)"}, {"WEB_CONCURRENCY": "0"}, {"GUNICORN_THREADS": "99"},
    {"GUNICORN_TIMEOUT": "9"}, {"PORT": "99999999999999999999999"}])
def test_launcher_rejects_invalid_values_without_starting_anything(launcher, variables):
    result, arguments = launcher(**variables)
    assert result.returncode == 64 and arguments == []


def test_web_launcher_preserves_gunicorn_exit_status(launcher):
    result, arguments = launcher(FAKE_EXIT_CODE="37")
    assert result.returncode == 37
    assert arguments[arguments.index("--bind") + 1] == "0.0.0.0:8000"


def test_maintenance_runs_once_and_stops_on_error(launcher):
    result, commands = launcher("maintenance", FAIL_COMMAND="purge_restore_sessions")
    assert result.returncode == 9
    assert commands == ["purge_trash", "purge_restore_sessions"]


def test_maintenance_finishes_after_all_four_commands(launcher):
    result, commands = launcher("maintenance")
    assert result.returncode == 0, result.stderr
    assert commands == ["purge_trash", "purge_restore_sessions", "purge_login_attempts", "clearsessions"]


def build_environment():
    return {key: value for key, value in os.environ.items()
            if not key.startswith("DJANGO_") and key not in {"DATABASE_URL", "SECRET_KEY"}}


def test_collectstatic_build_has_no_runtime_credentials_and_never_connects_to_database(tmp_path):
    output = tmp_path / "static"
    script = '''
import sys
sys.argv = ["manage.py", "collectstatic"]
import django
from django.db.backends.base.base import BaseDatabaseWrapper
def forbidden(*args, **kwargs):
    raise AssertionError("Static build attempted a database connection")
BaseDatabaseWrapper.connect = forbidden
django.setup()
from django.conf import settings
from django.core.management import call_command
from django.test import override_settings
with override_settings(STATIC_ROOT=OUTPUT):
    call_command("collectstatic", interactive=False, verbosity=0)
assert settings.SECRET_KEY.startswith("build-only-")
print("static build without database passed")
'''.replace("OUTPUT", repr(str(output)))
    env = {**build_environment(), "DJANGO_SETTINGS_MODULE": "config.build_settings"}
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "staticfiles.json").read_text(encoding="utf-8"))
    hashed = manifest["paths"]["tseh/app.css"]
    assert hashed != "tseh/app.css" and (output / hashed).is_file()
    assert (output / (hashed + ".gz")).is_file()


def test_runtime_cannot_inherit_build_credentials_or_use_build_settings():
    for module in ("config.settings", "config.build_settings"):
        result = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=ROOT,
            env=build_environment(), capture_output=True, text=True, timeout=15)
        assert result.returncode != 0
        assert "DJANGO_SECRET_KEY" in result.stderr if module == "config.settings" else "restricted to collectstatic" in result.stderr


@pytest.fixture
def release(monkeypatch):
    spec = spec_from_file_location("release_command_test", ROOT / "scripts" / "predeploy.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    from tseh.models import DomainState
    events, options = [], {"locked": True, "exists": True, "atomic": True, "fail": False}
    class Cursor:
        query = ""
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, query, params=None):
            self.query = query
            events.append(query)
        def fetchone(self):
            return (options["locked"],) if "pg_try_advisory_lock" in self.query else ("state" if options["exists"] else None,)
    @contextmanager
    def atomic():
        events.append("begin")
        try:
            yield
        except Exception:
            events.append("rollback")
            raise
        else:
            events.append("commit")
    state = SimpleNamespace(revision=7, save=lambda **kwargs: events.append("save revision"))
    def lock():
        events.append("lock state")
        return SimpleNamespace(get=lambda **kwargs: state)
    def migrate(*args, **kwargs):
        events.append("migrate")
        if options["fail"]:
            raise RuntimeError("migration failed")
    monkeypatch.setattr(module, "connection", SimpleNamespace(cursor=Cursor, close=lambda: events.append("close")))
    monkeypatch.setattr(module, "transaction", SimpleNamespace(atomic=atomic))
    monkeypatch.setattr(module, "MigrationExecutor", lambda conn: SimpleNamespace(
        loader=SimpleNamespace(graph=SimpleNamespace(leaf_nodes=lambda: ["leaf"])),
        migration_plan=lambda targets: [(SimpleNamespace(atomic=options["atomic"]), False)]))
    monkeypatch.setattr(module, "call_command", migrate)
    monkeypatch.setattr(DomainState.objects, "select_for_update", lock)
    return module, events, options, state


def test_release_locks_existing_state_before_migration_and_invalidates_forms(release):
    module, events, _, state = release
    module.deploy_database()
    assert events.index("begin") < events.index("lock state") < events.index("migrate") < events.index("save revision") < events.index("commit")
    assert state.revision == 8 and events[-1] == "close"


def test_release_failure_closes_lock_and_does_not_invalidate_uncommitted_state(release):
    module, events, options, state = release
    options["fail"] = True
    with pytest.raises(RuntimeError, match="migration failed"):
        module.deploy_database()
    assert "rollback" in events and "save revision" not in events
    assert state.revision == 7 and events[-1] == "close"


@pytest.mark.parametrize("key", ["locked", "atomic"])
def test_release_refuses_concurrent_or_non_atomic_migration_before_writes(release, key):
    module, events, options, state = release
    options[key] = False
    with pytest.raises(CommandError):
        module.deploy_database()
    assert "migrate" not in events and "save revision" not in events and state.revision == 7
    assert events[-1] == "close"


def test_first_release_creates_schema_before_locking_new_state(release):
    module, events, options, state = release
    options["exists"] = False
    module.deploy_database()
    assert events.index("migrate") < events.index("lock state")
    assert state.revision == 8 and events[-1] == "close"


def real_release_module():
    spec = spec_from_file_location("real_release_command_test", ROOT / "scripts" / "predeploy.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.django_db(transaction=True)
def test_postgresql_release_preserves_data_and_rejects_old_forms():
    from tseh.models import DomainState, Supplier
    from tseh.services.commands import StaleWrite, save_supplier, stamp
    state, _ = DomainState.objects.get_or_create(pk=1)
    old_revision, old_epoch = state.revision, state.epoch
    token = stamp(state)
    existing = Supplier.objects.create(name="Preserved across release")
    real_release_module().deploy_database()
    state.refresh_from_db()
    assert state.revision == old_revision + 1 and state.epoch == old_epoch
    assert Supplier.objects.get(pk=existing.pk).name == "Preserved across release"
    with pytest.raises(StaleWrite):
        save_supplier(token, {"name": "Must not be created", "note": ""})
    assert Supplier.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_postgresql_release_rolls_back_ddl_data_and_releases_advisory_lock(monkeypatch):
    from django.db import connection
    from tseh.models import DomainState, Supplier
    state, _ = DomainState.objects.get_or_create(pk=1)
    old_revision = state.revision
    module = real_release_module()
    def broken_migration(*args, **kwargs):
        Supplier.objects.create(name="Uncommitted migration data")
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE stage7_failed_migration_probe (id integer)")
        raise RuntimeError("Simulated release failure")
    monkeypatch.setattr(module, "call_command", broken_migration)
    with pytest.raises(RuntimeError, match="Simulated release failure"):
        module.deploy_database()
    state.refresh_from_db()
    assert state.revision == old_revision and Supplier.objects.count() == 0
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('stage7_failed_migration_probe')")
        assert cursor.fetchone()[0] is None
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [module.MIGRATION_LOCK])
        assert cursor.fetchone()[0] is True
        cursor.execute("SELECT pg_advisory_unlock(%s)", [module.MIGRATION_LOCK])


@pytest.mark.django_db(transaction=True)
def test_postgresql_concurrent_release_is_refused_without_mutation():
    from django.db import connection
    from tseh.models import DomainState
    state, _ = DomainState.objects.get_or_create(pk=1)
    old_revision = state.revision
    module = real_release_module()
    other = connection.copy(alias="release_lock_probe")
    try:
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [module.MIGRATION_LOCK])
        with pytest.raises(CommandError, match="Another release"):
            module.deploy_database()
        state.refresh_from_db()
        assert state.revision == old_revision
    finally:
        other.close()


@pytest.mark.django_db(transaction=True)
def test_first_postgresql_release_creates_schema_without_demo_data_or_user():
    from django.contrib.auth import get_user_model
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from tseh.models import Dish, DomainState, Ingredient, Supplier
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    executor.migrate([(app, None) for app in sorted({app for app, _ in latest})])
    try:
        real_release_module().deploy_database()
        assert DomainState.objects.get(pk=1).revision == 1
        assert all(model.objects.count() == 0 for model in (Dish, Ingredient, Supplier, get_user_model()))
        assert MigrationExecutor(connection).migration_plan(latest) == []
    finally:
        MigrationExecutor(connection).migrate(latest)
