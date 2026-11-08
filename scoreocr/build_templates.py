"""Build glyph templates from labelled scoreboards.

Start from glyphs that split cleanly, then repeat: average templates, force-align every labelled
cell to its known string, re-average. Run: python -m scoreocr.build_templates
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

from . import glyphs as G
from .badges import Icons, badge_ink, logo_ink
from .layout import STAT_COLS, cell, find_layout, row_background

FIXTURES = Path(__file__).parent.parent / "tests" / "fixtures"
ICON_ASSET = Path(__file__).parent / "assets" / "icons.npz"


def labelled_cells(image: Path, labels: dict) -> list[tuple[np.ndarray, str, bool]]:
    rgb = np.asarray(Image.open(image).convert("RGB"))
    lay = find_layout(rgb)
    players = [p for t in labels["teams"] for p in t["players"]]
    out = []
    for row, p in zip(lay.rows, players, strict=True):
        bg = row_background(rgb, lay, row)
        for key, col in STAT_COLS.items():
            ink, burst = G.ink_map(cell(rgb, lay, row.center, col, G.ZOOM), bg)
            out.append((G.center_vertically(ink), p[key], burst))
    return out


def _split(ink: np.ndarray) -> list[np.ndarray]:
    on = ink.max(axis=0) > 0.25
    d = np.diff(np.concatenate(([0], on.astype(np.int8), [0])))
    return [
        ink[:, a:b] for a, b in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1), strict=True)
    ]


def _average(groups: dict[str, list[np.ndarray]], h: int) -> G.Templates:
    res = {}
    for ch, parts in groups.items():
        w = max(p.shape[1] for p in parts)
        stack = np.zeros((len(parts), h, w), dtype=np.float32)
        for i, p in enumerate(parts):
            x = (w - p.shape[1]) // 2
            stack[i, :, x : x + p.shape[1]] = p
        t = stack.mean(axis=0)
        ys = np.flatnonzero(t.max(axis=1) > 0.15)
        xs = np.flatnonzero(t.max(axis=0) > 0.15)
        res[ch] = t[ys[0] : ys[-1] + 1, xs[0] : xs[-1] + 1]
    return G.Templates(res)


def build(cells: list[tuple[np.ndarray, str, bool]], rounds: int = 4) -> G.Templates:
    plain = [c for c in cells if not c[2]]
    h = plain[0][0].shape[0]
    samples: dict[str, list[np.ndarray]] = {c: [] for c in G.ALPHABET}
    for ink, text, _ in plain:
        parts = _split(ink)
        if len(parts) == len(text):
            for ch, part in zip(text, parts, strict=True):
                samples[ch].append(part)
    missing = [c for c, v in samples.items() if not v]
    if missing:
        raise ValueError(f"no clean samples for glyphs: {missing}")

    tpl = _average(samples, h)
    for _ in range(rounds):
        groups: dict[str, list[np.ndarray]] = {c: [] for c in G.ALPHABET}
        for ink, text, _b in plain:
            _, _, seq = G.decode(ink, tpl, forced=text)
            if len(seq) == len(text):
                for ch, x in seq:
                    groups[ch].append(ink[:, x : x + tpl.glyphs[ch].shape[1]])
        tpl = _average({k: (v or samples[k]) for k, v in groups.items()}, h)
    return tpl


def build_icons(image: Path, labels: dict) -> Icons:
    """Average each badge over its labelled rows. Logos come from each team's header."""
    rgb = np.asarray(Image.open(image).convert("RGB"))
    lay = find_layout(rgb)
    players = [p for t in labels["teams"] for p in t["players"]]
    arrays: dict[str, np.ndarray] = {}
    for badge in ("key", "mvp"):
        inks = [
            badge_ink(rgb, lay, row)
            for row, p in zip(lay.rows, players, strict=True)
            if p["badge"] == badge
        ]
        arrays[badge] = np.mean(inks, axis=0)
    slots = {"Hidden King": "logo_hidden_king", "Archmother": "logo_archmother"}
    for team, first in zip(labels["teams"], (lay.rows[0], lay.rows[6]), strict=True):
        arrays[slots[team["name"]]] = logo_ink(rgb, lay, first)
    return Icons(arrays)


def main() -> None:
    labels = json.loads((FIXTURES / "scoreboard_sample.json").read_text(encoding="utf-8"))
    tpl = build(labelled_cells(FIXTURES / "scoreboard_sample.webp", labels))
    tpl.save()
    print({k: v.shape for k, v in tpl.glyphs.items()}, G.ASSET.stat().st_size, "bytes")
    icons = build_icons(FIXTURES / "scoreboard_sample.webp", labels)
    icons.save()
    print(list(icons.arrays), ICON_ASSET.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
