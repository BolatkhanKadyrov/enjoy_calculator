"""Local-only Windows helper. No system service, no default password, no production data."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / ".local"
BIN = LOCAL / "postgresql" / "pgsql" / "bin"
DATA = LOCAL / "pgdata"
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def settings():
    path = LOCAL / "dev.json"
    if not path.exists():
        LOCAL.mkdir(exist_ok=True)
        path.write_text(json.dumps({"password": secrets.token_urlsafe(32), "secret": secrets.token_urlsafe(64), "port": 55432}), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def environment(database="tseh"):
    config = settings()
    return {**os.environ, "DJANGO_SECRET_KEY": config["secret"], "DJANGO_DEBUG": "1",
        "DJANGO_SECURE_SSL_REDIRECT": "0", "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
        "DATABASE_URL": f"postgresql://tseh:{quote(config['password'])}@127.0.0.1:{config['port']}/{database}"}


def pg(*args, check=True):
    # Files instead of pipes: Windows PostgreSQL children may inherit pipe handles.
    with (LOCAL / "pgctl.log").open("ab") as log:
        result = subprocess.run([str(BIN / "pg_ctl.exe"), *args], stdout=log, stderr=log, creationflags=FLAGS)
    print((LOCAL / "pgctl.log").read_text(encoding="utf-8", errors="replace")[-2000:])
    if check:
        result.check_returncode()
    return result


def start():
    if not (BIN / "initdb.exe").exists():
        raise SystemExit("Portable PostgreSQL not found. See README.md or supply your own DATABASE_URL.")
    config = settings()
    if not (DATA / "PG_VERSION").exists():
        password_file = LOCAL / "init-password.tmp"
        password_file.write_text(config["password"], encoding="utf-8")
        try:
            subprocess.run([str(BIN / "initdb.exe"), "-D", str(DATA), "-U", "tseh", "--auth=scram-sha-256",
                "--encoding=UTF8", "--locale=C", f"--pwfile={password_file}"], check=True, creationflags=FLAGS)
        finally:
            password_file.unlink(missing_ok=True)
    status = subprocess.run([str(BIN / "pg_ctl.exe"), "-D", str(DATA), "status"], capture_output=True, creationflags=FLAGS)
    if status.returncode != 0:
        pg("-D", str(DATA), "-l", str(LOCAL / "postgres.log"), "-o", f"-h 127.0.0.1 -p {config['port']}", "-w", "start")
    import psycopg
    with psycopg.connect(environment("postgres")["DATABASE_URL"], autocommit=True, connect_timeout=5) as conn:
        for name in ["tseh", "tseh_qa"]:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
                from psycopg import sql
                conn.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0").format(sql.Identifier(name)))
    print("Local PostgreSQL is ready at 127.0.0.1:" + str(config["port"]))


def main():
    args = sys.argv[1:]
    if not args:
        raise SystemExit("Usage: python scripts/dev.py start-db | stop-db | test [args] | manage [args] | qa [args]")
    action = args.pop(0)
    if action == "start-db":
        start()
        return
    if action == "stop-db":
        pg("-D", str(DATA), "-m", "fast", "stop")
        return
    env = environment("tseh_qa" if action == "qa" else "tseh")
    if action == "test":
        command = [sys.executable, "-m", "pytest", *args]
    elif action in {"manage", "qa"}:
        command = [sys.executable, "manage.py", *args]
    else:
        raise SystemExit("Unknown command")
    raise SystemExit(subprocess.call(command, cwd=ROOT, env=env))


if __name__ == "__main__":
    main()
