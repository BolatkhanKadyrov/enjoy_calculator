import os
from pathlib import Path
import subprocess
import sys

import pytest


def configuration_result(**changes):
    env = {**os.environ, "DJANGO_DEBUG": "0", "DJANGO_ALLOWED_HOSTS": "example.test,healthcheck.railway.app",
           "DJANGO_SECRET_KEY": "test-production-only-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
           "DJANGO_CSRF_TRUSTED_ORIGINS": "https://example.test", "DJANGO_TRUST_PROXY": "0",
           "DJANGO_SECURE_SSL_REDIRECT": "1", "DATABASE_URL": "postgresql://example:example@127.0.0.1:1/example"}
    for key, value in changes.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run([sys.executable, "-c", "from config import settings; print('configuration OK')"],
                          env=env, cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True)


def test_database_url_security_options_preserved():
    env = {**os.environ, "DATABASE_URL": "postgresql://example:example@localhost/example?sslmode=verify-full&connect_timeout=9"}
    check = "from config.settings import DATABASES; o=DATABASES['default']['OPTIONS']; assert o['sslmode']=='verify-full'; assert o['connect_timeout']==9"
    result = subprocess.run([sys.executable, "-c", check], env=env, cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_production_configuration_import_needs_no_database_connection():
    assert configuration_result().returncode == 0


@pytest.mark.parametrize("changes, message", [
    ({"DJANGO_SECRET_KEY": "short"}, "DJANGO_SECRET_KEY"),
    ({"DJANGO_SECRET_KEY": "a" * 64}, "strong random"),
    ({"DJANGO_SECRET_KEY": "django-insecure-" + "abcdefghi" * 6}, "strong random"),
    ({"DJANGO_ALLOWED_HOSTS": None}, "explicit DJANGO_ALLOWED_HOSTS"),
    ({"DJANGO_ALLOWED_HOSTS": "*"}, "wildcard"),
    ({"DJANGO_CSRF_TRUSTED_ORIGINS": "http://example.test"}, "HTTPS origins"),
    ({"DJANGO_CSRF_TRUSTED_ORIGINS": "https://*.example.test"}, "explicit HTTPS"),
    ({"DJANGO_SECURE_SSL_REDIRECT": "0"}, "requires HTTPS"),
    ({"DJANGO_TRUST_PROXY": "true"}, "must be 0 or 1"),
    ({"DJANGO_DEBUG": "False"}, "must be 0 or 1"),
    ({"DATABASE_URL": None}, "valid PostgreSQL"),
    ({"DATABASE_URL": "sqlite:///:memory:"}, "requires PostgreSQL"),
    ({"DJANGO_LOGIN_MAX_ATTEMPTS": "0"}, "positive integer"),
    ({"DJANGO_LOGIN_WINDOW_SECONDS": "nan"}, "positive integer"),
    ({"DJANGO_LOGIN_WINDOW_SECONDS": "999999999999999999"}, "must not exceed 86400"),
    ({"DJANGO_LOGIN_MAX_ATTEMPTS": "1001"}, "must not exceed 1000"),
])
def test_unsafe_or_malformed_configuration_fails_closed(changes, message):
    result = configuration_result(**changes)
    assert result.returncode != 0
    assert message in result.stderr


def test_malformed_database_configuration_does_not_echo_password():
    result = configuration_result(DATABASE_URL="bogus://user:do-not-echo-this-password@localhost/db")
    assert result.returncode != 0
    assert "do-not-echo-this-password" not in result.stderr


def test_build_settings_refuse_web_or_arbitrary_management_commands():
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.build_settings"}
    result = subprocess.run([sys.executable, "manage.py", "check"], env=env,
                            cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True)
    assert result.returncode != 0
    assert "restricted to collectstatic" in result.stderr


@pytest.mark.django_db
def test_production_https_proxy_health_and_security_headers(client, settings):
    settings.DEBUG = False
    settings.ALLOWED_HOSTS = ["testserver", "healthcheck.railway.app"]
    settings.SECURE_SSL_REDIRECT = True
    settings.SECURE_PROXY_SSL_HEADER = None
    settings.SECURE_HSTS_SECONDS = 31536000
    settings.CSRF_COOKIE_SECURE = True
    # Forged proxy headers cannot suppress HTTPS redirection in the default mode.
    redirect = client.get("/login/", HTTP_X_FORWARDED_PROTO="https")
    assert redirect.status_code == 301
    assert redirect["Location"] == "https://testserver/login/"
    health = client.get("/health/", HTTP_HOST="healthcheck.railway.app")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert "no-store" in health["Cache-Control"]
    assert client.get("/health/extra").status_code == 301
    assert client.get("/health/", HTTP_HOST="attacker.test").status_code == 400
    settings.SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    response = client.get("/login/", HTTP_X_FORWARDED_PROTO="https")
    assert response.status_code == 200
    assert response.cookies["csrftoken"]["secure"]
    assert response["Strict-Transport-Security"] == "max-age=31536000"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["X-Frame-Options"] == "DENY"
    assert response["Referrer-Policy"] == "same-origin"
    assert "no-store" in response["Cache-Control"]


@pytest.mark.django_db
def test_private_domain_pages_and_redirects_are_not_cacheable(authenticated, state):
    from django.test import Client
    anonymous = Client().get("/data/")
    assert anonymous.status_code == 302
    for response in [authenticated.get("/"), authenticated.get("/data/"), anonymous]:
        assert "no-store" in response["Cache-Control"]
        assert "private" in response["Cache-Control"]
