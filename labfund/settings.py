"""Settings for LabFund — runs locally on SQLite and on Railway with Postgres (DATABASE_URL)."""
import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = os.environ.get("DEBUG", "1") == "1"

ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")
CSRF_TRUSTED_ORIGINS = [
    f"https://{d}" for d in [os.environ.get("RAILWAY_PUBLIC_DOMAIN")] if d
]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "budget",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "budget.middleware.FxAutoRefreshMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "labfund.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "budget.context.version",
            ],
        },
    },
]

WSGI_APPLICATION = "labfund.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=600
    )
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "he"
TIME_ZONE = "Asia/Jerusalem"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# With DEBUG off Django mails request errors to admins (none configured) and prints nothing, so a 500 on
# Railway left no trace. Put the traceback on stdout, where Railway's logs show it.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "budget": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"
DATA_UPLOAD_MAX_NUMBER_FIELDS = 20000

# Exchange rates refresh themselves on page loads (see budget/fx.py)
FX_AUTO_REFRESH = os.environ.get("FX_AUTO_REFRESH", "1") == "1"
FX_REFRESH_MINUTES = int(os.environ.get("FX_REFRESH_MINUTES", "60"))

# Resolve engine defaults (all overridable per run from the UI / CLI)
RESOLVE_DEFAULTS = {
    "lookback_days": 180,     # ignore stale 'ordered' rows older than this
    "fx_margin": 0.03,        # safety margin when order currency != grant currency
    "urgency_halflife_days": 90,
    "w_urgency": 0.6,
    "w_reserve": 0.5,
    "w_priority": 0.1,
    "w_fx": 0.05,
    "respect_labels": False,  # True = orders already labelled in LabSuit keep that grant
}

# Automatic update from LabSuit (robot). The page each dashboard's export starts from — the robot logs in
# there, clicks "Export", confirms the site's own "Export" popup, and waits for the file.
LABSUIT_URLS = {
    "orders": os.environ.get("LABSUIT_ORDERS_URL", "https://www.app.labsuit.com/dashboard/?labId=3861#orders/status[]=approved"),
}
AUTOFETCH_DOWNLOAD_SECONDS = int(os.environ.get("AUTOFETCH_DOWNLOAD_SECONDS", "60"))
AUTOFETCH_SYNC = False        # tests run the job inline instead of in a background thread
# The robot also saves the PDF attached to every request (orders only). Files we already hold are skipped.
AUTOFETCH_PDFS = os.environ.get("AUTOFETCH_PDFS", "1") == "1"
AUTOFETCH_PDF_SECONDS = int(os.environ.get("AUTOFETCH_PDF_SECONDS", "1500"))     # total time budget for the PDFs
AUTOFETCH_PDF_REFETCH = os.environ.get("AUTOFETCH_PDF_REFETCH", "0") == "1"      # 1 = fetch all again
AUTOFETCH_PDF_PANEL_SECONDS = int(os.environ.get("AUTOFETCH_PDF_PANEL_SECONDS", "6"))  # wait for the side panel's file
