from io import StringIO
from unittest.mock import patch
import warnings
import getpass

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError

from tseh.models import AccountAccess, AccountEvent

pytestmark = pytest.mark.django_db
PASSWORD = "owner-bootstrap-secret-83!"


@pytest.fixture(autouse=True)
def fast_password_hash(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def run_create(passwords=(PASSWORD, PASSWORD), **options):
    stdout, stderr = StringIO(), StringIO()
    with patch("tseh.management.commands.create_owner.getpass.getpass", side_effect=passwords) as prompt:
        call_command("create_owner", stdout=stdout, stderr=stderr, **options)
    return stdout.getvalue() + stderr.getvalue(), prompt.call_count


def test_create_first_owner_as_ordinary_django_user_without_secret_output():
    output, prompts = run_create()
    user = get_user_model().objects.get(username="tseh")
    assert user.is_active and user.check_password(PASSWORD)
    assert not user.is_staff and not user.is_superuser
    assert user.account_access.is_owner and user.account_access.permissions == []
    assert prompts == 2
    assert PASSWORD not in output and user.password not in output
    assert "Администратор tseh создан" in output
    assert not AccountEvent.objects.exists()


def test_custom_login_is_canonical_and_owner_is_not_username_magic():
    run_create(username="  WORKSHOP.Owner  ")
    user = get_user_model().objects.get(username="workshop.owner")
    assert user.account_access.is_owner


@pytest.mark.parametrize("active", [True, False])
def test_existing_owner_is_preserved_without_prompt(active):
    owner = get_user_model().objects.create_user(username="existing-owner", password=PASSWORD, is_active=active)
    AccountAccess.objects.create(user=owner, is_owner=True)
    old_hash = owner.password
    with patch("tseh.management.commands.create_owner.getpass.getpass") as prompt:
        with pytest.raises(CommandError, match="уже существует"):
            call_command("create_owner", stdout=StringIO())
    prompt.assert_not_called()
    owner.refresh_from_db()
    assert owner.password == old_hash and owner.is_active == active
    assert get_user_model().objects.count() == 1


@pytest.mark.parametrize("username,active", [("tseh", True), ("TSEH", False)])
def test_existing_ordinary_account_is_never_overwritten_or_promoted(username, active):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD, is_active=active)
    original_hash = user.password
    with patch("tseh.management.commands.create_owner.getpass.getpass") as prompt:
        with pytest.raises(CommandError, match="Логин уже занят"):
            call_command("create_owner", stdout=StringIO())
    prompt.assert_not_called()
    user.refresh_from_db()
    assert user.password == original_hash and user.is_active == active
    assert not AccountAccess.objects.exists()
    assert get_user_model().objects.count() == 1


@pytest.mark.parametrize("passwords", [("", ""), (PASSWORD, "wrong"), ("short", "short"),
                                      ("123456789012", "123456789012")])
def test_password_validation_leaves_no_partial_owner(passwords):
    stdout, stderr = StringIO(), StringIO()
    with patch("tseh.management.commands.create_owner.getpass.getpass", side_effect=passwords):
        with pytest.raises(CommandError):
            call_command("create_owner", stdout=stdout, stderr=stderr)
    assert not get_user_model().objects.exists() and not AccountAccess.objects.exists()
    assert all(not password or password not in stdout.getvalue() + stderr.getvalue() for password in passwords)


def test_owner_created_while_password_is_entered_is_detected_before_writing():
    owner = None
    def prompt(_):
        nonlocal owner
        if owner is None:
            owner = get_user_model().objects.create_user(username="concurrent-owner", password=PASSWORD)
            AccountAccess.objects.create(user=owner, is_owner=True)
        return PASSWORD
    with patch("tseh.management.commands.create_owner.getpass.getpass", side_effect=prompt):
        with pytest.raises(CommandError, match="уже существует"):
            call_command("create_owner", stdout=StringIO())
    assert get_user_model().objects.count() == 1
    assert not get_user_model().objects.filter(username="tseh").exists()


def test_missing_secure_terminal_refuses_getpass_echo_fallback():
    def fallback(_):
        warnings.warn("Password input may be echoed.", getpass.GetPassWarning)
        raise AssertionError("Unsafe input must not proceed")
    with patch("tseh.management.commands.create_owner.getpass.getpass", side_effect=fallback):
        with pytest.raises(CommandError, match="безопасным вводом"):
            call_command("create_owner", stdout=StringIO())
    assert not get_user_model().objects.exists() and not AccountAccess.objects.exists()


@pytest.mark.parametrize("username", ["ab", "../owner", "владелец", "owner name", "a" * 151])
def test_invalid_login_fails_before_password_prompt(username):
    with patch("tseh.management.commands.create_owner.getpass.getpass") as prompt:
        with pytest.raises(CommandError):
            call_command("create_owner", username=username, stdout=StringIO())
    prompt.assert_not_called()
    assert not get_user_model().objects.exists()
