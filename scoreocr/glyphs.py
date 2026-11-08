"""Read a numeric cell by fitting glyph templates left to right, with no prior segmentation.

A dynamic program picks the sequence of templates that best reconstructs the cell's ink map, so
glyphs that touch (the `k` suffix often does) are not a problem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ZOOM = 1
ALPHABET = "0123456789.k"
ASSET = Path(__file__).parent / "assets" / "glyphs.npz"
VALUE_RE = re.compile(r"^(\d{1,3}k?|\d\.\dk)$", re.ASCII)
# Costs are squared ink error summed over pixels, so these depend on ZOOM.
GLYPH_PENALTY = 0.15  # discourages explaining one glyph as several narrow ones
DOT_PENALTY = 0.025
GAP_PENALTY = 0.07
Y_SLACK = 2
DECIMAL_GAP = 2  # px between digits: true two-digit values measured 0-2, a missed dot 3


@dataclass(frozen=True)
class Reading:
    text: str
    confidence: float
    burst: bool = False
    source: str = "template"
    maybe_decimal: bool = False


def ink_map(crop: np.ndarray, bg: np.ndarray | None = None) -> tuple[np.ndarray, bool]:
    """Turn an RGB cell into 0..1 ink. Handles plain, dimmed, selected and starburst cells.

    `bg` is the row's background colour. Pass it for stat cells: a starburst can be wider than a
    narrow column, so the cell's own edges are not reliably background.
    """
    h, w, _ = crop.shape
    if bg is None:
        side = max(1, w // 10)
        bg = np.median(
            np.concatenate([crop[:, :side].reshape(-1, 3), crop[:, -side:].reshape(-1, 3)]), axis=0
        )
    dist = np.linalg.norm(crop - bg, axis=2)
    # A starburst fills the area directly above and below the text with one flat colour.
    band = max(1, h // 8)
    mid = crop[:, w // 3 : 2 * w // 3]
    ring = np.concatenate([mid[:band].reshape(-1, 3), mid[-band:].reshape(-1, 3)])
    ring_c = np.median(ring, axis=0)
    burst = bool(np.linalg.norm(ring_c - bg) > 25)
    if burst:
        dist = np.minimum(dist, np.linalg.norm(crop - ring_c, axis=2))
    hi = float(np.percentile(dist, 99))
    if hi < 15:
        return np.zeros((h, w), dtype=np.float32), burst
    lo = 0.45 * hi if burst else 0.12 * hi
    return np.clip((dist - lo) / (hi - lo), 0, 1).astype(np.float32), burst


def center_vertically(ink: np.ndarray) -> np.ndarray:
    prof = ink.sum(axis=1)
    if prof.sum() <= 0:
        return ink
    cy = float((prof * np.arange(len(prof))).sum() / prof.sum())
    return np.roll(ink, round(ink.shape[0] / 2 - 0.5 - cy), axis=0)


class Templates:
    def __init__(self, glyphs: dict[str, np.ndarray]):
        self.glyphs = glyphs

    @classmethod
    def load(cls, path: Path = ASSET) -> Templates:
        with np.load(path) as z:
            return cls({("." if k == "dot" else k): z[k].astype(np.float32) for k in z.files})

    def save(self, path: Path = ASSET) -> None:
        arrays = {("dot" if k == "." else k): v.astype(np.float16) for k, v in self.glyphs.items()}
        np.savez_compressed(path, **arrays)


def _fit_costs(ink: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Best squared error of template t placed at each x, over small vertical shifts."""
    h, w = ink.shape
    th, tw = t.shape
    if tw > w or th > h:
        return np.full(0, np.inf)
    win = np.lib.stride_tricks.sliding_window_view(ink, (th, tw))
    mid = (h - th) // 2
    ys = range(max(0, mid - Y_SLACK), min(win.shape[0], mid + Y_SLACK + 1))
    return np.min([((win[y] - t) ** 2).sum(axis=(1, 2)) for y in ys], axis=0)


# Grammar of a cell as a small automaton: 1-3 digits, optional k, or d.dk.
_DIGIT = "0123456789"
_NEXT: dict[int, dict[str, int]] = {
    0: dict.fromkeys(_DIGIT, 1),
    1: {**dict.fromkeys(_DIGIT, 2), ".": 4, "k": 6},
    2: {**dict.fromkeys(_DIGIT, 3), "k": 6},
    3: {"k": 6},
    4: dict.fromkeys(_DIGIT, 5),
    5: {"k": 6},
    6: {},
}
_ACCEPT = (1, 2, 3, 6)


def _chain(text: str) -> tuple[dict[int, dict[str, int]], tuple[int, ...]]:
    return {i: {c: i + 1} for i, c in enumerate(text)} | {len(text): {}}, (len(text),)


def decode(
    ink: np.ndarray, tpl: Templates, forced: str | None = None
) -> tuple[str, float, list[tuple[str, int]]]:
    """Return (text, confidence, placements). With `forced`, align to exactly that string."""
    columns = np.flatnonzero(ink.max(axis=0) > 0.05)
    if columns.size == 0:
        return "", 0.0, []
    # Only the inked span matters. Keep room so a template can still straddle its faint edge.
    left = max(0, int(columns[0]) - 2)
    ink_c = ink[:, left : int(columns[-1]) + 3]
    w = ink_c.shape[1]
    blank = (ink_c**2).sum(axis=0)
    prefix = np.concatenate(([0.0], np.cumsum(blank)))
    total = float(prefix[-1])
    nxt, accept = _chain(forced) if forced is not None else (_NEXT, _ACCEPT)
    done = max(nxt) + 1

    # Placements worse than leaving those columns blank can never win, so drop them up front.
    # Forced alignment keeps every placement because it has to place the given glyph regardless.
    options: list[list[tuple[str, int, float]]] = [[] for _ in range(w)]
    for g, t in tpl.glyphs.items():
        tw = t.shape[1]
        costs = _fit_costs(ink_c, t) + (DOT_PENALTY if g == "." else GLYPH_PENALTY)
        if forced is None:
            useful = np.flatnonzero(costs < prefix[tw:] - prefix[:-tw])
        else:
            useful = np.arange(len(costs))
        for x in useful.tolist():
            options[x].append((g, tw, float(costs[x])))

    best = np.full((w + 1, done + 1), np.inf)
    best[0, 0] = 0.0
    back: dict[tuple[int, int], tuple[int, int, str | None]] = {}

    def relax(x: int, s: int, value: float, frm: tuple[int, int], g: str | None) -> None:
        if value < best[x, s]:
            best[x, s] = value
            back[(x, s)] = (*frm, g)

    for x in range(w + 1):
        for s in nxt:
            cur = best[x, s]
            if not np.isfinite(cur):
                continue
            if s in accept:
                relax(x, done, cur, (x, s), None)
            if x == w:
                continue
            # Blank columns are free before the first glyph; inside a value they cost extra, so
            # a wide gap has to be explained by a decimal point.
            relax(x + 1, s, cur + blank[x] + (GAP_PENALTY if s else 0.0), (x, s), None)
            for g, tw, c in options[x]:
                if g in nxt[s]:
                    relax(x + tw, nxt[s][g], cur + c, (x, s), g)
        if x < w and np.isfinite(best[x, done]):
            relax(x + 1, done, best[x, done] + blank[x], (x, done), None)

    end = (w, done)
    if not np.isfinite(best[end]):
        return "", 0.0, []
    seq: list[tuple[str, int]] = []
    state = end
    while state != (0, 0):
        px, ps, g = back[state]
        if g is not None:
            seq.append((g, px + left))
        state = (px, ps)
    seq.reverse()
    text = "".join(g for g, _ in seq)
    return text, float(max(0.0, 1.0 - best[end] / max(total, 1e-6))), seq


def read_cell(crop: np.ndarray, tpl: Templates, bg: np.ndarray | None = None) -> Reading:
    ink, burst = ink_map(crop, bg)
    text, conf, seq = decode(center_vertically(ink), tpl)
    if not VALUE_RE.match(text):
        conf = 0.0
    maybe_decimal = False
    if re.fullmatch(r"\d\dk", text, re.ASCII):
        first, first_x = seq[0]
        maybe_decimal = seq[1][1] - (first_x + tpl.glyphs[first].shape[1]) >= DECIMAL_GAP
    return Reading(text, conf, burst, maybe_decimal=maybe_decimal)
