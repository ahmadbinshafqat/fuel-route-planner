from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="route-map", permanent=False)),
    path("api/route/", views.route_plan, name="route-plan"),
    path("api/route/map/", views.route_map, name="route-map"),
    path("api/places/", views.place_suggestions, name="place-suggestions"),
    path("api/health/", views.health, name="health"),
]
