import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    value = os.environ.get(name, "1" if default else "0")
    if value not in {"0", "1"}:
        raise ImproperlyConfigured(f"{name} must be 0 or 1.")
    return value == "1"


def env_positive_int(name, default, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be a positive integer.") from None
    if value < 1:
        raise ImproperlyConfigured(f"{name} must be a positive integer.")
    if value > maximum:
        raise ImproperlyConfigured(f"{name} must not exceed {maximum}.")
    return value


DEBUG = env_bool("DJANGO_DEBUG")
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if len(SECRET_KEY) < 32:
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY to a random secret of at least 32 characters.")
if not DEBUG and (len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 5 or SECRET_KEY.startswith("django-insecure-")):
    raise ImproperlyConfigured("Production DJANGO_SECRET_KEY must be a strong random secret of at least 50 characters.")
ALLOWED_HOSTS = [x.strip() for x in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "").split(",") if x.strip()]
if not DEBUG and (not ALLOWED_HOSTS or any("*" in host for host in ALLOWED_HOSTS)):
    raise ImproperlyConfigured("Set explicit DJANGO_ALLOWED_HOSTS for production; wildcard hosts are not permitted.")
CSRF_TRUSTED_ORIGINS = [x.strip() for x in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if x.strip()]
if not DEBUG and any(not origin.startswith("https://") or "*" in origin for origin in CSRF_TRUSTED_ORIGINS):
    raise ImproperlyConfigured("Production CSRF origins must be explicit HTTPS origins.")
try:
    DATABASES = {"default": dj_database_url.parse(os.environ["DATABASE_URL"], conn_max_age=60, conn_health_checks=True)}
except (KeyError, ValueError):
    raise ImproperlyConfigured("Set a valid PostgreSQL DATABASE_URL.") from None
DATABASES["default"].setdefault("OPTIONS", {}).setdefault("connect_timeout", 5)
if DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
    raise ImproperlyConfigured("This application requires PostgreSQL, including for integration tests.")
INSTALLED_APPS = [
    "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    "django.contrib.messages", "django.contrib.staticfiles", "tseh",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware", "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware", "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware", "django.contrib.auth.middleware.AuthenticationMiddleware",
    "tseh.middleware.PrivateResponseMiddleware",
    "tseh.middleware.AccountAccessMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware", "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [], "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["django.template.context_processors.request", "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages", "tseh.access.access_context"]}}]
LANGUAGE_CODE = "ru"
TIME_ZONE = os.environ.get("TZ", "Asia/Qyzylorda")
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", not DEBUG)
if not DEBUG and not SECURE_SSL_REDIRECT:
    raise ImproperlyConfigured("Production requires HTTPS; keep DJANGO_SECURE_SSL_REDIRECT=1.")
# Opt in only behind an ingress that overwrites these headers and is the sole public path.
DJANGO_TRUST_PROXY = env_bool("DJANGO_TRUST_PROXY")
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if DJANGO_TRUST_PROXY else None
# Railway probes over HTTP. This exact, data-free endpoint must not redirect.
SECURE_REDIRECT_EXEMPT = [r"^health/$"]
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Up to 100 scenario rows with new-price flags and signed form metadata.
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000
LOGIN_MAX_ATTEMPTS = env_positive_int("DJANGO_LOGIN_MAX_ATTEMPTS", 10, 1000)
LOGIN_WINDOW_SECONDS = env_positive_int("DJANGO_LOGIN_WINDOW_SECONDS", 300, 86400)
