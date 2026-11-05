"""Manual local upgrade: verified pg_dump backup before schema migrations."""
from datetime import datetime, timezone
import os
import subprocess
import sys
import uuid

import psycopg
from psycopg import sql

from dev import BIN, FLAGS, LOCAL, ROOT, environment, settings

config = settings()
backup_dir = LOCAL / "backups"
backup_dir.mkdir(exist_ok=True)
archive = backup_dir / ("tseh-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".dump")
env = {**os.environ, "PGPASSWORD": config["password"]}
connection_args = ["-h", "127.0.0.1", "-p", str(config["port"]), "-U", "tseh"]
verification_database = "tseh_verify_" + uuid.uuid4().hex
with psycopg.connect(environment()["DATABASE_URL"], connect_timeout=5) as guard:
    guard.execute("SET LOCAL lock_timeout = '15s'")
    # All application writes and form snapshots take this row lock. It also protects
    # callers who invoke this script without stopping their local web server first.
    guard.execute("SELECT revision FROM tseh_domainstate WHERE id=1 FOR UPDATE").fetchone()
    try:
        subprocess.run([str(BIN / "pg_dump.exe"), *connection_args, "-d", "tseh", "-Fc", "-f", str(archive)],
                       env=env, check=True, creationflags=FLAGS)
        with psycopg.connect(environment("postgres")["DATABASE_URL"], autocommit=True, connect_timeout=5) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(verification_database)))
            try:
                subprocess.run([str(BIN / "pg_restore.exe"), *connection_args, "--exit-on-error", "--no-owner", "--no-privileges",
                                "-d", verification_database, str(archive)], env=env, check=True, creationflags=FLAGS)
            finally:
                # Only the database with the fresh identifier created above is removed.
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(verification_database)))
    except (subprocess.CalledProcessError, OSError, psycopg.Error):
        raise SystemExit("Backup or trial restore failed. Application database migration was not started.")
    print("Backup and trial restore passed: " + str(archive), flush=True)
    result = subprocess.run([sys.executable, str(ROOT / "manage.py"), "migrate", "--noinput"], cwd=ROOT, env=environment())
    # Invalidate open pre-upgrade forms even if a migration failed after an earlier one committed.
    guard.execute("UPDATE tseh_domainstate SET revision=revision+1 WHERE id=1")
    guard.commit()
raise SystemExit(result.returncode)
