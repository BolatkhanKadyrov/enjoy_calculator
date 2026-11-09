"""Private HTML, errors and downloads must not become shared/browser cache entries."""
from django.utils.cache import add_never_cache_headers
from django.contrib.auth import HASH_SESSION_KEY, get_user_model, logout
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.crypto import constant_time_compare

from tseh.access import Access, authorize
from tseh.audit_actor import audit_actor


class AccountAccessMiddleware:
    """Live checks before handlers, with serialized POST/revocation ordering."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and request.method == 'POST':
            from tseh.models import AccountAccess
            with transaction.atomic():
                list(AccountAccess.objects.select_for_update().filter(is_owner=True).order_by('pk'))
                profile = AccountAccess.objects.select_for_update().filter(user_id=request.user.pk).first()
                request.user = get_user_model().objects.get(pk=request.user.pk)
                # Authentication may have happened before waiting for these
                # locks. Recheck revoked sessions/passwords after the wait.
                session_key = request.session.session_key
                if (not session_key or not request.session.exists(session_key)
                        or not constant_time_compare(request.session.get(HASH_SESSION_KEY, ''),
                                                     request.user.get_session_auth_hash())):
                    logout(request)
                    raise PermissionDenied('Вход завершён. Войдите снова.')
                request.access = Access(request.user, profile)
                with audit_actor(request.user.get_username()):
                    response = self.get_response(request)
                if response.status_code >= 500:
                    transaction.set_rollback(True)
                return response
        request.access = Access(request.user)
        with audit_actor(request.user.get_username() if request.user.is_authenticated else ''):
            return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not request.user.is_authenticated:
            return None
        if not request.user.is_active:
            raise PermissionDenied
        route = request.resolver_match.url_name
        if route == 'dashboard' and request.method in {'GET', 'HEAD'} and not request.access.can('dishes.view'):
            if request.access.home_url == reverse('access-unavailable'):
                return render(request, 'tseh/access_unavailable.html', status=403)
            return redirect(request.access.home_url)
        authorize(request, route, view_kwargs)
        return None


class PrivateResponseMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # WhiteNoise serves immutable static assets earlier in the middleware chain.
        # Keep the small readiness response fresh as well; it contains no domain data.
        add_never_cache_headers(response)
        return response
