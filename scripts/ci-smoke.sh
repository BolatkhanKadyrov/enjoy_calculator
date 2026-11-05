#!/usr/bin/env bash
# Linux/Docker smoke only. Creates its own disposable PostgreSQL and ignores any
# host DATABASE_URL or Django credentials. Does not publish/deploy an image.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    printf '%s\n' 'Usage: bash scripts/ci-smoke.sh LOCAL_IMAGE' >&2
    exit 64
fi
image=$1
docker image inspect "$image" >/dev/null
[[ $(docker image inspect --format '{{.Config.User}}' "$image") == '10001:10001' ]]

run_id="tseh-ci-$(date +%s)-$$-$RANDOM"
label="io.tseh.ci-run=$run_id"
network="$run_id-net"
postgres="$run_id-postgres"
web="$run_id-web"
predeploy="$run_id-predeploy"
verify="$run_id-verify"
maintenance="$run_id-maintenance"
containers=("$web" "$maintenance" "$verify" "$predeploy" "$postgres")

cleanup() {
    status=$?
    trap - EXIT
    # Exact names plus an ownership label: never prune or select unrelated data.
    for container in "${containers[@]}"; do
        owner=$(docker container inspect --format '{{index .Config.Labels "io.tseh.ci-run"}}' "$container" 2>/dev/null || true)
        if [[ $owner == "$run_id" ]]; then
            if [[ $status -ne 0 ]]; then
                printf '\nSmoke failure: last logs for %s\n' "$container" >&2
                docker logs --tail 80 "$container" >&2 || true
            fi
            docker rm --force --volumes "$container" >/dev/null || true
        fi
    done
    owner=$(docker network inspect --format '{{index .Labels "io.tseh.ci-run"}}' "$network" 2>/dev/null || true)
    if [[ $owner == "$run_id" ]]; then
        docker network rm "$network" >/dev/null || true
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker network create --label "$label" "$network" >/dev/null
docker run --detach --name "$postgres" --label "$label" \
    --network "$network" --network-alias postgres \
    --tmpfs /var/lib/postgresql/data:rw \
    --env POSTGRES_USER=tseh_ci \
    --env POSTGRES_PASSWORD=ci-only-disposable-password \
    --env POSTGRES_DB=tseh_ci \
    --env 'POSTGRES_INITDB_ARGS=--encoding=UTF8 --locale=C' \
    postgres:17-bookworm >/dev/null

ready=0
for _ in {1..60}; do
    if docker exec "$postgres" pg_isready -h 127.0.0.1 -U tseh_ci -d tseh_ci >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
[[ $ready == 1 ]] || { printf '%s\n' 'PostgreSQL did not become ready.' >&2; exit 1; }
[[ $(docker exec "$postgres" psql -U tseh_ci -d tseh_ci -Atc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'") == 0 ]]

# Intentionally public, CI-only values. The isolated DB contains no user data.
app_env=(
    --env DATABASE_URL=postgresql://tseh_ci:ci-only-disposable-password@postgres:5432/tseh_ci
    --env DJANGO_SECRET_KEY=ci-only-secret-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ-abcdefghijklmnopqrstuvwxyz
    --env DJANGO_DEBUG=0
    --env DJANGO_SECURE_SSL_REDIRECT=1
    --env DJANGO_TRUST_PROXY=1
    --env DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,healthcheck.railway.app,ci.example.test
    --env DJANGO_CSRF_TRUSTED_ORIGINS=https://ci.example.test
    --env TZ=Asia/Qyzylorda
    --env PORT=8087
)

timeout 300 docker run --rm --name "$predeploy" --label "$label" \
    --network "$network" "${app_env[@]}" "$image" python scripts/predeploy.py

timeout 60 docker run --rm --name "$verify" --label "$label" \
    --network "$network" "${app_env[@]}" "$image" python -c '
import os
import django
django.setup()
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from tseh.models import Dish, DomainState, Ingredient, Preparation, Supplier
assert os.geteuid() == 10001
assert connection.pg_version // 10000 == 17
executor = MigrationExecutor(connection)
assert executor.migration_plan(executor.loader.graph.leaf_nodes()) == []
assert DomainState.objects.get(pk=1).revision == 1
assert all(model.objects.count() == 0 for model in (Dish, Ingredient, Preparation, Supplier, get_user_model()))
print("Fresh PostgreSQL 17 schema is migrated; no demo data or account was created.")
'

timeout 60 docker run --rm --name "$verify" --label "$label" \
    --network "$network" "${app_env[@]}" "$image" python manage.py check --deploy

docker run --detach --name "$web" --label "$label" \
    --network "$network" --publish 127.0.0.1::8087 \
    "${app_env[@]}" "$image" >/dev/null
address=$(docker port "$web" 8087/tcp)
[[ $address =~ ^127\.0\.0\.1:[0-9]+$ ]]

# This exercises proxy handling with simulated headers, not Railway's real edge.
python3 - "http://$address" <<'PY'
import gzip
from html.parser import HTMLParser
from http.client import HTTPException
from http.cookies import SimpleCookie
import json
import re
import sys
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

base = sys.argv[1]
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

opener = build_opener(ProxyHandler({}), NoRedirect())
def request(path, secure=False, health_host=False, compressed=False):
    headers = {"Host": "healthcheck.railway.app" if health_host else "ci.example.test"}
    if secure:
        headers.update({"X-Forwarded-Proto": "https", "X-Real-IP": "198.51.100.12"})
    if compressed:
        headers["Accept-Encoding"] = "gzip"
    try:
        response = opener.open(Request(base + path, headers=headers), timeout=10)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read()

for attempt in range(60):
    try:
        status, headers, body = request("/health/", health_host=True)
        if status == 200:
            break
    # Docker can accept a connection before the Gunicorn worker is listening.
    # Retry startup resets/disconnects only here; later assertions remain strict.
    except (OSError, HTTPException):
        pass
    time.sleep(1)
else:
    raise AssertionError("Gunicorn did not become healthy")
assert json.loads(body) == {"status": "ok"}
assert "no-store" in headers["Cache-Control"]

status, headers, _ = request("/login/")
assert status == 301 and headers["Location"] == "https://ci.example.test/login/"
status, headers, body = request("/login/", secure=True)
assert status == 200 and "gunicorn" in headers["Server"].lower()
assert headers["Strict-Transport-Security"] == "max-age=31536000"
assert headers["X-Content-Type-Options"] == "nosniff"
assert headers["X-Frame-Options"] == "DENY"
assert headers["Referrer-Policy"] == "same-origin"
assert "no-store" in headers["Cache-Control"]
cookies = SimpleCookie()
for value in headers.get_all("Set-Cookie", []):
    cookies.load(value)
assert cookies["csrftoken"]["secure"] and cookies["csrftoken"]["samesite"] == "Lax"

class Assets(HTMLParser):
    urls = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.urls.append(attrs["href"])
        if tag == "script" and attrs.get("src"):
            self.urls.append(attrs["src"])

assets = Assets()
assets.feed(body.decode("utf-8"))
assert assets.urls and any(url.endswith(".css") for url in assets.urls)
assert any(url.endswith(".js") for url in assets.urls)
for url in assets.urls:
    assert re.fullmatch(r"/static/tseh/[^/?]+\.[0-9a-f]{12}\.(css|js)", url), url
    status, headers, content = request(url, secure=True, compressed=True)
    assert status == 200, (url, status)
    assert "immutable" in headers["Cache-Control"], url
    if url.endswith(".css"):
        assert "text/css" in headers["Content-Type"]
        assert headers["Content-Encoding"] == "gzip"
    if headers.get("Content-Encoding") == "gzip":
        content = gzip.decompress(content)
    assert content, url
print("Gunicorn: health over HTTP, HTTPS redirect/headers/cookie and hashed WhiteNoise assets passed.")
PY

docker exec "$web" python -c '
import os
from pathlib import Path
assert os.geteuid() == 10001
status = Path("/proc/1/status").read_text()
assert next(line for line in status.splitlines() if line.startswith("Uid:")).split()[1:] == ["10001"] * 4
assert b"gunicorn" in Path("/proc/1/cmdline").read_bytes()
print("Gunicorn master and runtime commands run as UID 10001.")
'

timeout 120 docker run --rm --name "$maintenance" --label "$label" \
    --network "$network" "${app_env[@]}" "$image" sh scripts/start-production.sh maintenance
docker exec "$web" python -c '
import django
django.setup()
from django.contrib.auth import get_user_model
from tseh.models import DomainState
assert DomainState.objects.get(pk=1).revision == 1
assert get_user_model().objects.count() == 0
print("Web startup and maintenance did not migrate again or create an account.")
'

# A DB outage must produce a controlled 503, not a healthy-looking placeholder.
docker stop --time 10 "$postgres" >/dev/null
python3 - "http://$address/health/" <<'PY'
import json
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
opener = build_opener(ProxyHandler({}))
try:
    response = opener.open(Request(sys.argv[1], headers={"Host": "healthcheck.railway.app"}), timeout=15)
except HTTPError as error:
    response = error
with response:
    assert response.status == 503
    assert json.loads(response.read()) == {"status": "unavailable"}
print("Unavailable PostgreSQL is reported as HTTP 503.")
PY

printf '%s\n' 'Linux production smoke passed; removing only this run’s containers and network.'
