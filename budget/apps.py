import os

from django.apps import AppConfig
from django.db.models.signals import post_migrate

DEFAULT_ADMIN_USERNAME = os.environ.get("DEFAULT_ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD", "admin")


def create_default_admin(sender, **kwargs):
    """After `migrate`: if the site has no users at all, create a known admin user.

    Defaults to admin / admin; override with DEFAULT_ADMIN_USERNAME / DEFAULT_ADMIN_PASSWORD.
    Runs only while the users table is empty, so it never resets a password that was changed.
    """
    from django.contrib.auth.models import User

    if User.objects.exists():
        return
    User.objects.create_superuser(DEFAULT_ADMIN_USERNAME, email="", password=DEFAULT_ADMIN_PASSWORD)


def flag_default_password(sender, request, user, **kwargs):
    """Remember in the session if someone logged in with the default password, to show a reminder."""
    request.session["default_password"] = (
        user.username == DEFAULT_ADMIN_USERNAME and user.check_password(DEFAULT_ADMIN_PASSWORD))


class BudgetConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "budget"

    def ready(self):
        from django.contrib.auth.signals import user_logged_in

        post_migrate.connect(create_default_admin, sender=self)
        user_logged_in.connect(flag_default_password)
