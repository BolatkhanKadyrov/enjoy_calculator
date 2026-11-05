"""Explicitly create a throwaway browser-review account in tseh_qa only."""
import os
import sys
from dev import ROOT, environment

sys.path.insert(0, str(ROOT))
os.environ.update(environment("tseh_qa"))
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
import django
django.setup()
from django.conf import settings
from django.core.management import call_command
from django.contrib.auth import get_user_model

if settings.DATABASES["default"]["NAME"] != "tseh_qa" or not settings.DEBUG:
    raise SystemExit("QA account is allowed only in isolated tseh_qa with DEBUG enabled.")
if sys.argv[1:] == ["--remove-account"]:
    get_user_model().objects.filter(username="qa-review").delete()
    print("Temporary QA account removed.")
    raise SystemExit(0)
password = os.environ.get("TSEH_QA_PASSWORD", "")
if len(password) < 20:
    raise SystemExit("Set a temporary TSEH_QA_PASSWORD of at least 20 characters.")
call_command("migrate", interactive=False, verbosity=0)
user, created = get_user_model().objects.get_or_create(username="qa-review")
user.set_password(password)
user.save()
print("Isolated QA account prepared. No domain data was added.")
