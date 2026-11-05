from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from threading import Barrier
from unittest.mock import patch

import pytest
from django.contrib.auth import SESSION_KEY
from django.core.management import call_command
from django.db import DatabaseError, close_old_connections, connections
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from tseh.auth import (
    LoginLimited, cleanup_login_attempts, login_source_key,
    release_login_attempt, reserve_login_attempt,
)
from tseh.models.security import LoginThrottle


pytestmark = pytest.mark.django_db
PASSWORD = "test-only-strong-password"


@pytest.fixture(autouse=True)
def security_settings(settings):
    settings.LOGIN_MAX_ATTEMPTS = 10
    settings.LOGIN_WINDOW_SECONDS = 300
    settings.DJANGO_TRUST_PROXY = False
    # These tests exercise access controls, not the password hashing algorithm.
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def post_login(client, *, username="test-team", password="incorrect-secret-value", **extra):
    return client.post(reverse("login"), {"username": username, "password": password}, **extra)


def source_key(**meta):
    return login_source_key(RequestFactory().post("/login/", **meta))


def test_get_and_csrf_rejection_do_not_consume_attempts():
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse("login")).status_code == 200
    assert post_login(client).status_code == 403
    assert not LoginThrottle.objects.exists()
    token = client.cookies["csrftoken"].value
    assert post_login(client, HTTP_X_CSRFTOKEN=token).status_code == 200
    assert LoginThrottle.objects.get().attempts == 1


def test_ten_failures_then_block_before_authentication(client, user):
    for _ in range(10):
        assert post_login(client).status_code == 200
    with patch("django.contrib.auth.forms.authenticate") as authenticate:
        response = post_login(client, password=PASSWORD)
    authenticate.assert_not_called()
    assert response.status_code == 429
    assert 1 <= int(response["Retry-After"]) <= 300
    assert "Слишком много попыток" in response.content.decode()
    assert "incorrect-secret-value" not in response.content.decode()
    assert PASSWORD not in response.content.decode()
    assert response.context["form"].is_bound is False
    assert SESSION_KEY not in client.session
    assert LoginThrottle.objects.get().attempts == 10


def test_unknown_and_existing_user_have_same_throttle_response(user):
    responses = []
    for index, username in enumerate(("test-team", "does-not-exist")):
        client = Client(REMOTE_ADDR=f"192.0.2.{index + 1}")
        for _ in range(10):
            assert post_login(client, username=username).status_code == 200
        responses.append(post_login(client, username=username))
    assert all(response.status_code == 429 for response in responses)
    assert list(responses[0].context["form"].non_field_errors()) == list(responses[1].context["form"].non_field_errors())


def test_fixed_window_not_extended_by_rejections(client, user, monkeypatch):
    start = timezone.now()
    monkeypatch.setattr("tseh.auth.timezone.now", lambda: start)
    for _ in range(10):
        post_login(client)
    monkeypatch.setattr("tseh.auth.timezone.now", lambda: start + timedelta(seconds=299))
    assert post_login(client)["Retry-After"] == "1"
    assert LoginThrottle.objects.get().window_started_at == start
    monkeypatch.setattr("tseh.auth.timezone.now", lambda: start + timedelta(seconds=300))
    assert post_login(client).status_code == 200
    bucket = LoginThrottle.objects.get()
    assert bucket.attempts == 1
    assert bucket.window_started_at == start + timedelta(seconds=300)


def test_success_only_releases_own_attempt_and_safe_redirect(client, user):
    for _ in range(3):
        post_login(client)
    response = client.post(reverse("login"), {
        "username": user.username, "password": PASSWORD, "next": "https://example.invalid/steal",
    })
    assert response.status_code == 302
    assert response.url == reverse("dashboard")
    assert client.session[SESSION_KEY] == str(user.pk)
    assert LoginThrottle.objects.get().attempts == 3


def test_inactive_user_is_not_authenticated_and_consumes_attempt(client, user):
    user.is_active = False
    user.save(update_fields=["is_active"])
    assert post_login(client, password=PASSWORD).status_code == 200
    assert SESSION_KEY not in client.session
    assert LoginThrottle.objects.get().attempts == 1


def test_multiple_sessions_survive_shared_source_limit(user, state):
    first, second, attacker = Client(), Client(), Client()
    assert post_login(first, password=PASSWORD).status_code == 302
    assert post_login(second, password=PASSWORD).status_code == 302
    assert LoginThrottle.objects.get().attempts == 0
    for _ in range(10):
        post_login(attacker)
    assert post_login(attacker, password=PASSWORD).status_code == 429
    for client in (first, second):
        assert client.session[SESSION_KEY] == str(user.pk)
        assert client.get(reverse("dashboard")).status_code == 200
    elsewhere = Client(REMOTE_ADDR="192.0.2.100")
    assert post_login(elsewhere, password=PASSWORD).status_code == 302


def test_untrusted_forwarding_headers_cannot_select_bucket(client):
    expected = source_key(REMOTE_ADDR="127.0.0.1")
    for index in range(11):
        response = post_login(
            client, HTTP_X_REAL_IP=f"192.0.2.{index + 1}",
            HTTP_X_FORWARDED_FOR=f"198.51.100.{index + 1}",
            HTTP_FORWARDED=f"for=203.0.113.{index + 1}",
        )
    assert response.status_code == 429
    assert list(LoginThrottle.objects.values_list("source_key", flat=True)) == [expected]
    assert len(expected) == 64 and "127.0.0.1" not in expected


def test_explicit_trusted_ingress_uses_only_single_valid_real_ip(settings):
    settings.DJANGO_TRUST_PROXY = True
    peer = source_key(REMOTE_ADDR="100.64.0.1")
    assert source_key(REMOTE_ADDR="100.64.0.1", HTTP_X_REAL_IP="192.0.2.1") == source_key(REMOTE_ADDR="192.0.2.1")
    for invalid in ("", "not-an-ip", "192.0.2.1, 192.0.2.2", "192.0.2.1:80", "[::1]", "fe80::1%eth0", " " + "1" * 100):
        assert source_key(REMOTE_ADDR="100.64.0.1", HTTP_X_REAL_IP=invalid, HTTP_X_FORWARDED_FOR="192.0.2.9") == peer
    assert source_key(REMOTE_ADDR="100.64.0.1", HTTP_FORWARDED="for=192.0.2.9") == peer


def test_address_canonicalization_and_unknown_bucket():
    assert source_key(REMOTE_ADDR="::ffff:192.0.2.1") == source_key(REMOTE_ADDR="192.0.2.1")
    assert source_key(REMOTE_ADDR="2001:db8:abcd:1::1") == source_key(REMOTE_ADDR="2001:db8:abcd:1::ffff")
    assert source_key(REMOTE_ADDR="2001:db8:abcd:1::1") != source_key(REMOTE_ADDR="2001:db8:abcd:2::1")
    assert source_key(REMOTE_ADDR="invalid-one") == source_key(REMOTE_ADDR="invalid-two")
    assert source_key(REMOTE_ADDR=None) == source_key(REMOTE_ADDR="invalid-two")


def test_old_success_cannot_decrement_new_window():
    start = timezone.now()
    previous = reserve_login_attempt("a" * 64, now=start)
    current = reserve_login_attempt("a" * 64, now=start + timedelta(seconds=300))
    release_login_attempt(previous)
    assert LoginThrottle.objects.get().attempts == 1
    release_login_attempt(current)
    assert LoginThrottle.objects.get().attempts == 0


@pytest.mark.parametrize("operation", ["reserve_login_attempt", "release_login_attempt"])
def test_throttle_database_failure_is_closed_503(client, user, operation):
    with patch(f"tseh.auth.{operation}", side_effect=DatabaseError("test-only failure")):
        response = post_login(client, password=PASSWORD)
    assert response.status_code == 503
    assert response["Retry-After"] == "60"
    assert SESSION_KEY not in client.session
    assert PASSWORD not in response.content.decode()
    assert "test-only failure" not in response.content.decode()
    assert "Вход временно недоступен" in response.content.decode()


def test_authentication_database_failure_is_closed_503(client):
    with patch("django.contrib.auth.forms.authenticate", side_effect=DatabaseError("test-only failure")):
        response = post_login(client)
    assert response.status_code == 503
    assert SESSION_KEY not in client.session


def test_cleanup_only_expired_counters_and_command():
    now = timezone.now()
    LoginThrottle.objects.bulk_create([
        LoginThrottle(source_key="a" * 64, window_started_at=now - timedelta(days=2), attempts=10),
        LoginThrottle(source_key="b" * 64, window_started_at=now - timedelta(seconds=299), attempts=10),
    ])
    assert cleanup_login_attempts(now=now) == 1
    assert list(LoginThrottle.objects.values_list("source_key", flat=True)) == ["b" * 64]
    output = StringIO()
    call_command("purge_login_attempts", stdout=output)
    assert "0" in output.getvalue()


def test_cleanup_respects_a_configured_window_longer_than_a_day(settings):
    settings.LOGIN_WINDOW_SECONDS = 3 * 86400
    start = timezone.now() - timedelta(days=2)
    reserve_login_attempt("a" * 64, now=start)
    assert cleanup_login_attempts() == 0


def test_domain_backup_excludes_and_restore_preserves_security_state(user, state):
    from tseh.services import backups

    reservation = reserve_login_attempt("d" * 64)
    document = backups.export_snapshot()
    assert "login_throttle" not in document["data"]
    assert reservation.source_key.encode() not in backups.encode_backup(document)
    session = backups.prepare_restore(user, document)
    backups.download_backup(session.pk, user)
    backups.apply_restore(session.pk, user, review_token=backups.restore_token(session, user), confirm_download=True)
    bucket = LoginThrottle.objects.get()
    assert bucket.source_key == reservation.source_key
    assert bucket.attempts == 1
    assert bucket.window_started_at == reservation.window_started_at


@pytest.mark.django_db(transaction=True)
def test_concurrent_first_attempts_enforce_one_shared_limit(settings):
    settings.LOGIN_MAX_ATTEMPTS = 3
    barrier = Barrier(6)

    def attempt(_):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                reserve_login_attempt("c" * 64)
            except LoginLimited:
                return "limited"
            return "reserved"
        finally:
            # CONN_MAX_AGE retains healthy worker connections; explicitly close
            # them before Django drops the shared test database.
            connections.close_all()

    with ThreadPoolExecutor(max_workers=6) as executor:
        outcomes = list(executor.map(attempt, range(6)))
    assert outcomes.count("reserved") == 3
    assert outcomes.count("limited") == 3
    assert LoginThrottle.objects.get().attempts == 3
