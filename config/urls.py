from __future__ import annotations

from django.contrib import admin
from django.db import connection
from django.http import HttpRequest, JsonResponse
from django.urls import path

from league.web.views import calibration_dashboard


def healthz(_request: HttpRequest) -> JsonResponse:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok"})


urlpatterns = [
    # Before the admin's own URLs, so it isn't read as an app label.
    path("admin/calibration/", calibration_dashboard, name="calibration"),
    path("admin/", admin.site.urls),
    path("healthz", healthz),
]
