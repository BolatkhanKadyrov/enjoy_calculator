"""Interactive first-owner bootstrap, never an account promotion/reset command."""
import getpass
import re
import warnings

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, connection, transaction
from django.views.decorators.debug import sensitive_variables

from tseh.models import AccountAccess

BOOTSTRAP_LOCK = 0x545345484F574E52  # TSEHOWNR, transaction-scoped PostgreSQL lock.


class Command(BaseCommand):
    help = "Создать первого владельца Цеха с интерактивным вводом пароля. Существующие аккаунты не изменяются."
    requires_migrations_checks = True

    def add_arguments(self, parser):
        parser.add_argument("--username", default="tseh", help="Логин владельца (по умолчанию tseh).")

    def _check_available(self, username):
        if AccountAccess.objects.filter(is_owner=True).exists():
            raise CommandError("Владелец уже существует. Эта команда не меняет аккаунты и не создаёт второго владельца.")
        if get_user_model().objects.filter(username__iexact=username).exists():
            raise CommandError("Логин уже занят. Существующая учётная запись не будет изменена или повышена в правах.")

    @sensitive_variables("password", "confirmation")
    def handle(self, *args, **options):
        username = options["username"].strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,149}", username):
            raise CommandError("Логин: 3–150 латинских букв, цифр, точек, дефисов или подчёркиваний; первый символ — буква или цифра.")
        self._check_available(username)
        try:
            # Refuse getpass's echoing fallback when no safe terminal exists.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("Пароль владельца: ")
                confirmation = getpass.getpass("Повторите пароль: ")
        except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
            raise CommandError("Ввод отменён. Запустите команду в терминале с безопасным вводом пароля.") from None
        if not password or password != confirmation:
            raise CommandError("Пароли пусты или не совпадают. Владелец не создан.")
        user = get_user_model()(username=username, is_active=True, is_staff=False, is_superuser=False)
        try:
            validate_password(password, user)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from None
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", [BOOTSTRAP_LOCK])
                # Password entry can take time. Recheck under the shared lock.
                self._check_available(username)
                user.set_password(password)
                user.full_clean()
                user.save(force_insert=True)
                AccountAccess.objects.create(user=user, is_owner=True)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from None
        except IntegrityError:
            raise CommandError("Логин был занят другим процессом. Существующие аккаунты не изменены.") from None
        self.stdout.write(self.style.SUCCESS(f"Владелец {username} создан."))
