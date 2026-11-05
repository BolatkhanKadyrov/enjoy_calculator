"""A bounded PostgreSQL login limit without locking the shared account.

Only source HMACs and counters are stored, outside domain backups and journals.
Reserve before password checking, so concurrent workers cannot bypass the limit.
Successful authentication returns its own reservation, never earlier failures.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import ipaddress
import math

from django.conf import settings
from django.contrib.auth.views import LoginView
from django.db import DatabaseError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.crypto import salted_hmac

from tseh.models.security import LoginThrottle


@dataclass(frozen=True)
class LoginReservation:
    source_key: str
    window_started_at: datetime


class LoginLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__("Login source is temporarily limited.")


def _address(value):
    # No lists, ports, zone IDs or unbounded proxy input. Never parse XFF chains.
    if not isinstance(value, str) or len(value) > 45 or "%" in value:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def login_source_key(request):
    """Trust X-Real-IP only at an explicitly trusted HTTP ingress boundary."""
    address = None
    if settings.DJANGO_TRUST_PROXY:
        address = _address(request.META.get("HTTP_X_REAL_IP"))
    if address is None:
        address = _address(request.META.get("REMOTE_ADDR"))
    if address is None:
        source = "unknown"
    elif isinstance(address, ipaddress.IPv6Address):
        # Avoid bypass by rotating interface IDs within the same IPv6 subnet.
        source = "ipv6:" + str(ipaddress.ip_network((address, 64), strict=False))
    else:
        source = "ipv4:" + str(address)
    return salted_hmac("tseh-login-source-v1", source, algorithm="sha256").hexdigest()


def reserve_login_attempt(source_key, *, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        # get_or_create handles a competing first request through the unique PK.
        bucket, _ = LoginThrottle.objects.select_for_update().get_or_create(
            source_key=source_key, defaults={"window_started_at": now},
        )
        deadline = bucket.window_started_at + timedelta(seconds=settings.LOGIN_WINDOW_SECONDS)
        if now >= deadline:
            bucket.window_started_at = now
            bucket.attempts = 0
            deadline = now + timedelta(seconds=settings.LOGIN_WINDOW_SECONDS)
        if bucket.attempts >= settings.LOGIN_MAX_ATTEMPTS:
            # Rejections do not extend this fixed window or overwrite its start.
            raise LoginLimited(max(1, math.ceil((deadline - now).total_seconds())))
        bucket.attempts += 1
        bucket.save(update_fields=["window_started_at", "attempts"])
        return LoginReservation(source_key, bucket.window_started_at)


def release_login_attempt(reservation):
    # A slow successful request from an expired window cannot debit a new one.
    LoginThrottle.objects.filter(
        pk=reservation.source_key, window_started_at=reservation.window_started_at, attempts__gt=0,
    ).update(attempts=F("attempts") - 1)


def cleanup_login_attempts(*, now=None):
    now = now or timezone.now()
    retention = max(timedelta(days=1), timedelta(seconds=settings.LOGIN_WINDOW_SECONDS))
    return LoginThrottle.objects.filter(window_started_at__lt=now - retention).delete()[0]


class ThrottledLoginView(LoginView):
    """Keep Django's CSRF, sensitive POST handling, auth and safe redirects."""

    def _unavailable(self, *, retry_after=60, limited=False):
        # An unbound form cannot validate the password or echo credentials.
        kwargs = self.get_form_kwargs()
        kwargs.pop("data", None)
        kwargs.pop("files", None)
        form = self.get_form_class()(**kwargs)
        # Django add_error expects cleaned_data even for an unbound form.
        form.cleaned_data = {}
        message = (
            "Слишком много попыток входа. Подождите несколько минут и попробуйте снова."
            if limited else "Вход временно недоступен. Попробуйте чуть позже."
        )
        form.add_error(None, message)
        response = self.render_to_response(self.get_context_data(form=form), status=429 if limited else 503)
        response["Retry-After"] = str(retry_after)
        return response

    def post(self, request, *args, **kwargs):
        try:
            reservation = reserve_login_attempt(login_source_key(request))
        except LoginLimited as exc:
            return self._unavailable(retry_after=exc.retry_after, limited=True)
        except DatabaseError:
            return self._unavailable()

        form = self.get_form()
        try:
            valid = form.is_valid()
            if valid:
                release_login_attempt(reservation)
        except DatabaseError:
            return self._unavailable()
        # Leave Django's session mutation/error semantics intact: a failure
        # during auth_login must not be converted into a response that saves a
        # partially changed session. SessionMiddleware does not save on 5xx.
        return self.form_valid(form) if valid else self.form_invalid(form)

