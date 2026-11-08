from __future__ import annotations

from django.contrib import admin
from django.db import connection
from django.http import HttpRequest, JsonResponse
from django.urls import path


def healthz(_request: HttpRequest) -> JsonResponse:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthz),
]
