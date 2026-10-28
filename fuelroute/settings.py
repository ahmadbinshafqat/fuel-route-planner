"""Django settings for the fuelroute project."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "routing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.gzip.GZipMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "fuelroute.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    },
]

WSGI_APPLICATION = "fuelroute.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# Route/geocode results are cached so a repeated query costs zero external calls.
# Swap for Redis in production so the cache is shared across workers.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fuelroute",
        "TIMEOUT": 60 * 60 * 24,
        "OPTIONS": {"MAX_ENTRIES": 5000},
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Fuel route planner ---------------------------------------------------
DATA_DIR = BASE_DIR / "data"
FUEL_PRICES_CSV = DATA_DIR / "fuel-prices-for-be-assessment.csv"

FUEL_ROUTE = {
    "VEHICLE_RANGE_MILES": 500.0,
    "VEHICLE_MPG": 10.0,
    # Never plan to arrive at a pump or the finish with less than this in the tank.
    "RESERVE_GALLONS": 5.0,
    # Stations further than this from the route line are ignored.
    "CORRIDOR_MILES": 10.0,
    # Optimiser objective is fuel $ + STOP_PENALTY * stops. A few dollars stops the plan from
    # pulling over for 1-gallon top-ups that save cents; 0 gives the pure minimum-fuel-cost plan.
    "STOP_PENALTY_DOLLARS": 25.0,
    # A stop that would leave the tank within this many gallons of full fills it instead. The plan
    # stops short when the next pump is cheaper, but a few cents are not worth a driver's thought.
    "FILL_UP_WITHIN_GALLONS": 1.0,
    "OSRM_BASE_URL": os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org"),
    "NOMINATIM_BASE_URL": os.environ.get("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org"),
    "HTTP_TIMEOUT_SECONDS": 15,
    "USER_AGENT": "fuelroute-assessment/1.0 (Django)",
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"routing": {"handlers": ["console"], "level": "INFO"}},
}
