"""Collect static assets without production secrets or a reachable database."""
import os
import sys

from django.core.exceptions import ImproperlyConfigured

if len(sys.argv) < 2 or sys.argv[1] != "collectstatic":
    raise ImproperlyConfigured("config.build_settings is restricted to collectstatic.")

# These values exist only in the build process; never COPY an environment file into the image.
os.environ.update({
    "DJANGO_SECRET_KEY": "build-only-static-assets-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "DATABASE_URL": "postgresql://build:build@127.0.0.1:1/build",
    "DJANGO_DEBUG": "0",
    "DJANGO_ALLOWED_HOSTS": "build.invalid",
    "DJANGO_CSRF_TRUSTED_ORIGINS": "",
    "DJANGO_SECURE_SSL_REDIRECT": "1",
    "DJANGO_TRUST_PROXY": "0",
})

from .settings import *  # noqa: E402,F403
