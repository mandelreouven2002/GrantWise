from .fx import refresh_if_stale


class FxAutoRefreshMiddleware:
    """Keeps exchange rates fresh without a button: checked on every page load (GET, logged in)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "GET" and not request.path.startswith(("/static/", "/api/")) \
                and getattr(request, "user", None) is not None and request.user.is_authenticated:
            refresh_if_stale()
        return self.get_response(request)
