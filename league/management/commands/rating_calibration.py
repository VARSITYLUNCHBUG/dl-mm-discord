"""How well the ratings predict results, and what candidate parameters would have done.

Read-only. The staff dashboard at /admin/calibration/ shows the same numbers with charts.
"""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from league.models import Season
from league.services import calibration


class Command(BaseCommand):
    help = "Calibration of the season's ratings: Brier score, log loss, bins and side bias."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--season", type=int, help="Season ID. Defaults to the open one.")
        parser.add_argument("--params", help="JSON of candidate OpenSkill parameters to compare")
        parser.add_argument("--simulate", type=int, default=0, help="Run a synthetic league")
        parser.add_argument("--seeds", type=int, default=6, help="Simulation runs")

    def handle(self, *args: Any, **options: Any) -> None:
        candidate = json.loads(options["params"]) if options["params"] else None
        if options["simulate"]:
            runs = [
                calibration.simulate(options["simulate"], seed, candidate)
                for seed in range(1, options["seeds"] + 1)
            ]
            reached = ", ".join(str(r) if r is not None else "never" for r in runs)
            self.stdout.write(
                f"A true-35 player seeded as new reached mu 30 after: {reached} games "
                f"({options['seeds']} runs of {options['simulate']} matches)."
            )
            return
        season = (
            Season.objects.filter(pk=options["season"]).first()
            if options["season"]
            else Season.objects.filter(ended_at__isnull=True).order_by("-id").first()
        )
        if season is None:
            raise CommandError("No such season.")
        self.stdout.write(str(season))
        self._print("Live parameters", calibration.report(season))
        if candidate is not None:
            self._print("Candidate parameters", calibration.report(season, candidate))

    def _print(self, label: str, report: calibration.Report) -> None:
        self.stdout.write(f"\n{label}: {report.matches} rated matches")
        if not report.matches:
            return
        self.stdout.write(
            f"  Brier {report.brier:.4f} (always 50%: {calibration.BRIER_BASELINE:.4f}), "
            f"log loss {report.log_loss:.4f} ({calibration.LOG_LOSS_BASELINE:.4f})"
        )
        for b in report.bins:
            lo, hi = b.interval
            self.stdout.write(
                f"  predicted {b.low:.0%}-{b.high:.0%}: won {b.rate:.0%} of {b.games} "
                f"(95% {lo:.0%}-{hi:.0%})"
            )
        hk = report.hidden_king
        lo, hi = hk.interval
        self.stdout.write(
            f"  Hidden King won {hk.hits} of {hk.total} ({hk.share:.0%}, 95% {lo:.0%}-{hi:.0%})"
        )
        if report.top_mvps:
            top = ", ".join(f"{name} {count}" for name, count in report.top_mvps)
            self.stdout.write(f"  Most MVP awards: {top}")
        signal = report.awards_signal
        diff = signal.difference if signal else None
        if signal and signal.holders and signal.others and diff:
            self.stdout.write(
                f"  Next {signal.horizon} matches against prediction: award holders "
                f"{signal.holders.value * 100:+.1f} pts ({signal.holders.n}), others "
                f"{signal.others.value * 100:+.1f} pts. Difference {diff.value * 100:+.1f} "
                f"(roughly {diff.low * 100:+.1f} to {diff.high * 100:+.1f})"
            )
        for name, mvps, games in report.farming:
            self.stdout.write(f"  Award share frozen: {name}, {mvps} MVP in {games} matches")
