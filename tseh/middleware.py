"""Private HTML, errors and downloads must not become shared/browser cache entries."""
from django.utils.cache import add_never_cache_headers


class PrivateResponseMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # WhiteNoise serves immutable static assets earlier in the middleware chain.
        # Keep the small readiness response fresh as well; it contains no domain data.
        add_never_cache_headers(response)
        return response
