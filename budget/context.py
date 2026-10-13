from labfund import __version__


def version(request):
    """Version for the footer, and the saved LabSuit user for the automatic-update dialog."""
    ctx = {"LABFUND_VERSION": __version__}
    if getattr(request, "user", None) is not None and request.user.is_authenticated:
        from .models import SourceCredential
        cred = SourceCredential.objects.filter(source="labsuit").only("username").first()
        ctx["LABSUIT_USER"] = cred.username if cred else ""
    return ctx
