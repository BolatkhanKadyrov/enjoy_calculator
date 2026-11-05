#!/bin/sh
set -eu

# The web process never migrates, creates accounts or installs data at startup.
# A separately scheduled Railway service can use the explicit maintenance mode.
if [ "${1:-}" = "maintenance" ] && [ "$#" -eq 1 ]; then
    python manage.py purge_trash
    python manage.py purge_restore_sessions
    python manage.py purge_login_attempts
    exec python manage.py clearsessions
fi
if [ "$#" -ne 0 ]; then
    printf '%s\n' 'Usage: start-production.sh [maintenance]' >&2
    exit 64
fi

bounded_integer() {
    name=$1
    value=$2
    minimum=$3
    maximum=$4
    case "$value" in
        ''|*[!0-9]*) printf '%s\n' "$name must be an integer." >&2; exit 64 ;;
    esac
    if [ "${#value}" -gt 6 ] || [ "$value" -lt "$minimum" ] || [ "$value" -gt "$maximum" ]; then
        printf '%s\n' "$name is outside the supported range." >&2
        exit 64
    fi
}

PORT=${PORT:-8000}
WEB_CONCURRENCY=${WEB_CONCURRENCY:-2}
GUNICORN_THREADS=${GUNICORN_THREADS:-2}
GUNICORN_TIMEOUT=${GUNICORN_TIMEOUT:-120}
bounded_integer PORT "$PORT" 1 65535
bounded_integer WEB_CONCURRENCY "$WEB_CONCURRENCY" 1 16
bounded_integer GUNICORN_THREADS "$GUNICORN_THREADS" 1 8
bounded_integer GUNICORN_TIMEOUT "$GUNICORN_TIMEOUT" 10 600

# Only Django's explicit DJANGO_TRUST_PROXY opt-in interprets the edge's
# X-Forwarded-Proto. Do not add a second independent trust path in Gunicorn.
# The access log deliberately omits query strings, cookies and request bodies.
exec gunicorn config.wsgi:application \
    --bind "0.0.0.0:$PORT" \
    --workers "$WEB_CONCURRENCY" \
    --worker-class gthread \
    --threads "$GUNICORN_THREADS" \
    --timeout "$GUNICORN_TIMEOUT" \
    --graceful-timeout 30 \
    --forwarded-allow-ips '' \
    --access-logfile - \
    --access-logformat '%(m)s %(U)s %(s)s %(L)s' \
    --error-logfile - \
    --capture-output
