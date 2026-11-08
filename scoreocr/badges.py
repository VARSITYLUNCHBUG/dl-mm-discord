"""Icon matching for the key-player and MVP badges, and for the two team logos."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .layout import BADGE_COL, Layout, Row, cell, row_background

ASSET = Path(__file__).parent / "assets" / "icons.npz"
BADGE_HALF = 22
LOGO_COL = (84, 260)
LOGO_ABOVE = (66, 4)  # canonical px above the team's first row
BADGE_INK = 0.25
BADGE_MIN_PIXELS = 120  # empty slots measured 0, real badges 317 or more
MVP_MIN_HEIGHT = 34  # canonical px: key icons measured 27-28, MVP 39-41
BADGE_SLACK = 4
BADGE_MIN_SCORE = 0.6
LOGO_MIN_SCORE = 0.5

TEAM_LOGOS = {"logo_hidden_king": "Hidden King", "logo_archmother": "Archmother"}


def ncc(a: np.ndarray, b: np.ndarray) -> float:
    """Normalized correlation, -1..1. Flat input scores 0."""
    a = a - a.mean()
    b = b - b.mean()
    denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / denom) if denom > 1e-6 else 0.0


def badge_ink(rgb: np.ndarray, lay: Layout, row: Row) -> np.ndarray:
    crop = cell(rgb, lay, row.center, BADGE_COL, 1, half=BADGE_HALF)
    dist = np.linalg.norm(crop - row_background(rgb, lay, row), axis=2)
    return np.clip(dist / 150.0, 0, 1)


def logo_ink(rgb: np.ndarray, lay: Layout, first_row: Row) -> np.ndarray:
    above, below = LOGO_ABOVE
    half = (above - below) // 2
    center = first_row.top - (above + below) / 2 * lay.scale
    crop = cell(rgb, lay, center, LOGO_COL, 1, half=half)
    return crop.max(axis=2) / 255.0


@dataclass
class Icons:
    arrays: dict[str, np.ndarray]

    @classmethod
    def load(cls, path: Path = ASSET) -> Icons:
        with np.load(path) as z:
            return cls({k: z[k].astype(np.float32) for k in z.files})

    def save(self, path: Path = ASSET) -> None:
        np.savez_compressed(path, **{k: v.astype(np.float16) for k, v in self.arrays.items()})

    def badge(self, ink: np.ndarray) -> tuple[str | None, float]:
        """Return ("key" | "mvp" | "unknown" | None, score).

        Badge position shifts by a few pixels between captures, so exact-position matching is
        unreliable. An empty slot has no strong ink at all, and the MVP icon is much taller than
        the key icon, whichever row colour it sits on.
        """
        mask = ink > BADGE_INK
        if int(mask.sum()) < BADGE_MIN_PIXELS:
            return None, 1.0
        rows = np.flatnonzero(mask.sum(axis=1) >= 2)
        name = "mvp" if rows.size and rows[-1] - rows[0] + 1 >= MVP_MIN_HEIGHT else "key"
        score = max(
            ncc(np.roll(ink, (dy, dx), axis=(0, 1)), self.arrays[name])
            for dy in range(-BADGE_SLACK, BADGE_SLACK + 1)
            for dx in range(-BADGE_SLACK, BADGE_SLACK + 1)
        )
        # Something is in the slot but it is neither icon. The count checks will flag this.
        return (name if score >= BADGE_MIN_SCORE else "unknown"), score

    def team(self, ink: np.ndarray) -> tuple[str | None, float]:
        scores = {name: ncc(ink, self.arrays[key]) for key, name in TEAM_LOGOS.items()}
        name = max(scores, key=lambda n: scores[n])
        return (name if scores[name] >= LOGO_MIN_SCORE else None), scores[name]
