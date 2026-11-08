"""elo_v1: team-average Elo with one shared integer delta. Exactly zero-sum.

The rounding rule is part of the algorithm: replays must reproduce past results, so it is pinned
here rather than using Python's round(), which rounds halves to even.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

ALGO_VERSION = "elo_v1"
DIVISOR = 400


@dataclass(frozen=True, slots=True)
class EloResult:
    avg_a: float
    avg_b: float
    expected_a: float
    delta_a: int  # Team A players get +delta_a; team B players get -delta_a.


def expected_score(avg_a: float, avg_b: float) -> float:
    return 1 / (1 + 10 ** ((avg_b - avg_a) / DIVISOR))


def compute(ratings_a: Sequence[int], ratings_b: Sequence[int], a_won: bool, k: int) -> EloResult:
    if not ratings_a or not ratings_b:
        raise ValueError("Both teams need at least one player.")
    avg_a = sum(ratings_a) / len(ratings_a)
    avg_b = sum(ratings_b) / len(ratings_b)
    expected_a = expected_score(avg_a, avg_b)
    score_a = 1 if a_won else 0
    delta_a = math.floor(k * (score_a - expected_a) + 0.5)
    return EloResult(avg_a=avg_a, avg_b=avg_b, expected_a=expected_a, delta_a=delta_a)
