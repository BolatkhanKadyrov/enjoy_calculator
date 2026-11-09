from contextlib import contextmanager
from contextvars import ContextVar

_actor = ContextVar('tseh_audit_actor', default='')


def current_actor():
    return _actor.get()


@contextmanager
def audit_actor(username):
    token = _actor.set(username)
    try:
        yield
    finally:
        _actor.reset(token)
