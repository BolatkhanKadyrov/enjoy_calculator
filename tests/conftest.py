import pytest
from django.contrib.auth import get_user_model
from tseh.models import DomainState


@pytest.fixture(scope="session", autouse=True)
def built_static_assets():
    from django.core.management import call_command
    call_command("collectstatic", interactive=False, verbosity=0)


@pytest.fixture
def state(db):
    return DomainState.objects.get_or_create(pk=1)[0]


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="test-team", password="test-only-strong-password")


@pytest.fixture
def authenticated(client, user):
    client.force_login(user)
    return client
