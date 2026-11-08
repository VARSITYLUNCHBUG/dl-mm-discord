"""Locate the scoreboard table and cut cells, all relative to the detected row bands.

Coordinates in STAT_COLS and friends are "canonical" pixels: the reference sample's x positions,
where the row band runs 82..1858. Every image is mapped onto that frame, so mixed resolutions and
crops only change the scale and offset.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

CANON_W = 1776.0
CANON_X0 = 82.0
ROW_HALF = 13  # half height of a stat cell, canonical px

STAT_COLS: dict[str, tuple[int, int]] = {
    "souls": (484, 558),
    "k": (598, 648),
    "d": (645, 694),
    "a": (690, 741),
    "player_dmg": (772, 858),
    "obj_dmg": (878, 956),
    "healing": (980, 1058),
}
BG_COL = (1062, 1088)  # always empty: between the healing column and the items
BADGE_COL = (344, 388)
NAME_COL = (136, 340)
NAME_Y = (-22, 1)  # relative to the row centre
HERO_Y = (-1, 22)
TOTAL_COL = (470, 570)
TOTAL_OFFSET = 25  # below the team's last row
TOTAL_HALF = 15
DATE_COL = (1620, 1800)
DURATION_COL = (1806, 1862)
HEADER_Y = (30, 10)  # above the first row
MATCH_X = 1700  # runs to the image's right edge
MATCH_Y = (104, 80)
WINNER_COL = (240, 460)
WINNER_Y = (44, 10)  # above the team's first row


class NotAScoreboard(ValueError):
    """The image does not contain the 12 row bands."""


@dataclass(frozen=True)
class Row:
    top: float
    bottom: float

    @property
    def center(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass(frozen=True)
class Layout:
    rows: tuple[Row, ...]
    x0: float
    x1: float

    @property
    def scale(self) -> float:
        return (self.x1 - self.x0) / CANON_W

    def x(self, canon_x: float) -> float:
        return self.x0 + (canon_x - CANON_X0) * self.scale


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist(), strict=True))


def find_layout(rgb: np.ndarray) -> Layout:
    """Find 12 row bands from the row-wise median brightness, then their left and right edges."""
    gray = rgb.max(axis=2).astype(np.float32)
    h, w = gray.shape
    med = np.median(gray[:, int(w * 0.2) : int(w * 0.9)], axis=1)
    runs = [r for r in _runs(med > 25) if r[1] - r[0] >= h * 0.02]
    if len(runs) < 12:
        raise NotAScoreboard(f"found {len(runs)} row bands, expected 12")
    heights = np.array([b - a for a, b in runs], dtype=float)
    typical = float(np.median(heights))
    # Banners and panels can pass the brightness test too; they have the wrong height.
    runs = [r for r, t in zip(runs, heights, strict=True) if abs(t - typical) <= 0.12 * typical]
    if len(runs) != 12:
        raise NotAScoreboard(f"found {len(runs)} regular row bands, expected 12")
    tops = np.array([a for a, _ in runs], dtype=float)
    pitch = np.diff(tops)
    if np.ptp(pitch[:5]) > 0.1 * typical or np.ptp(pitch[6:]) > 0.1 * typical:
        raise NotAScoreboard("row bands are not evenly spaced")

    # Left and right edges: where the top strip of the bands stands out from the page. Icons sit
    # below that strip, and the median over rows ignores the selected-row highlight.
    strips = [gray[a + 1 : a + max(2, int(typical * 0.06))].mean(axis=0) for a, _ in runs]
    xs = _runs(np.median(np.stack(strips), axis=0) > 25)
    if not xs:
        raise NotAScoreboard("no band edges")
    x0, x1 = max(xs, key=lambda r: r[1] - r[0])
    if x1 - x0 < w * 0.4:
        raise NotAScoreboard("row bands are too narrow")
    return Layout(rows=tuple(Row(float(a), float(b)) for a, b in runs), x0=float(x0), x1=float(x1))


def resample(
    rgb: np.ndarray, x0: float, x1: float, y0: float, y1: float, nw: int, nh: int
) -> np.ndarray:
    """Bilinear crop of a float box into nw x nh."""
    xs = np.clip(x0 + (np.arange(nw) + 0.5) * (x1 - x0) / nw - 0.5, 0, rgb.shape[1] - 1)
    ys = np.clip(y0 + (np.arange(nh) + 0.5) * (y1 - y0) / nh - 0.5, 0, rgb.shape[0] - 1)
    xi = np.minimum(xs.astype(int), rgb.shape[1] - 2)
    yi = np.minimum(ys.astype(int), rgb.shape[0] - 2)
    fx = (xs - xi)[None, :, None]
    fy = (ys - yi)[:, None, None]
    a = rgb[np.ix_(yi, xi)].astype(np.float32)
    b = rgb[np.ix_(yi, xi + 1)].astype(np.float32)
    c = rgb[np.ix_(yi + 1, xi)].astype(np.float32)
    d = rgb[np.ix_(yi + 1, xi + 1)].astype(np.float32)
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy


def cell(
    rgb: np.ndarray,
    lay: Layout,
    center_y: float,
    col: tuple[int, int],
    zoom: int,
    half: int = ROW_HALF,
) -> np.ndarray:
    """Crop around a vertical centre, returned at canonical size times zoom."""
    s = lay.scale
    return resample(
        rgb,
        lay.x(col[0]),
        lay.x(col[1]),
        center_y - half * s,
        center_y + half * s,
        (col[1] - col[0]) * zoom,
        2 * half * zoom,
    )


def row_background(rgb: np.ndarray, lay: Layout, row: Row) -> np.ndarray:
    """The row's own background colour, sampled from a part of it that never holds content."""
    x0, x1 = round(lay.x(BG_COL[0])), round(lay.x(BG_COL[1]))
    y0, y1 = round(row.center - 8 * lay.scale), round(row.center + 8 * lay.scale)
    return np.median(rgb[y0:y1, x0:x1].reshape(-1, 3), axis=0).astype(np.float32)
