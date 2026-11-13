"""The staff calibration dashboard: how well ratings predict results, and how the data flows in."""

from __future__ import annotations

import json
from typing import Any

from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.safestring import mark_safe

from league.domain.openskill_v1 import DEFAULT_PARAMS
from league.models import Season
from league.services import calibration
from league.web.charts import calibration_svg, histogram_svg


def _season(request: HttpRequest) -> Season | None:
    seasons = Season.objects.select_related("community").order_by("-started_at")
    chosen = request.GET.get("season")
    if chosen and chosen.isdigit():
        return seasons.filter(pk=int(chosen)).first()
    return seasons.filter(ended_at__isnull=True).first() or seasons.first()


def _candidate(request: HttpRequest) -> tuple[dict[str, Any] | None, str]:
    """Candidate parameters from the form, and an error to show if they don't parse."""
    raw = request.GET.get("params", "").strip()
    if not raw:
        return None, ""
    try:
        params = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"That isn't JSON: {exc.msg}."
    if not isinstance(params, dict):
        return None, 'Give an object, such as {"beta": 3.5}.'
    unknown = sorted(set(params) - set(DEFAULT_PARAMS))
    if unknown:
        return None, f"Unknown parameters: {', '.join(unknown)}."
    return params, ""


def _points(value: float) -> str:
    return f"{value * 100:+.1f}"


def _signal(report: calibration.Report) -> dict[str, Any] | None:
    signal = report.awards_signal
    if signal is None or signal.holders is None or signal.others is None:
        return None
    diff = signal.difference
    assert diff is not None
    if diff.low > 0:
        verdict = "Award holders beat their predictions afterwards."
    elif diff.high < 0:
        verdict = "Award holders do worse than predicted afterwards."
    else:
        verdict = "No clear signal yet."
    return {
        "horizon": signal.horizon,
        "holders": _points(signal.holders.value),
        "holders_n": signal.holders.n,
        "others": _points(signal.others.value),
        "others_n": signal.others.n,
        "difference": _points(diff.value),
        "range": f"{_points(diff.low)} to {_points(diff.high)}",
        "verdict": verdict,
    }


def _view(report: calibration.Report) -> dict[str, Any]:
    return {
        "signal": _signal(report),
        "report": report,
        "calibration_svg": mark_safe(calibration_svg(report.bins)),
        "sigma_svg": mark_safe(histogram_svg(report.sigma_bins, "Uncertainty (sigma)")),
        # Only a margin that survives the rounding shown counts as better.
        "brier_better": report.brier <= calibration.BRIER_BASELINE - 0.001,
        "loss_better": report.log_loss <= calibration.LOG_LOSS_BASELINE - 0.001,
    }


@staff_member_required
def calibration_dashboard(request: HttpRequest) -> HttpResponse:
    season = _season(request)
    candidate, error = _candidate(request)
    context: dict[str, Any] = {
        **admin.site.each_context(request),
        "title": "Rating calibration",
        "seasons": Season.objects.select_related("community").order_by("-started_at"),
        "season": season,
        "params_text": request.GET.get("params", ""),
        "params_error": error,
        "brier_baseline": calibration.BRIER_BASELINE,
        "loss_baseline": calibration.LOG_LOSS_BASELINE,
    }
    if season is not None:
        context["live"] = _view(calibration.report(season))
        if candidate is not None:
            context["candidate"] = _view(calibration.report(season, candidate))
    return render(request, "league/calibration.html", context)
