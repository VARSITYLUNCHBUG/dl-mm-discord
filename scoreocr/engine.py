"""Read one scoreboard image into a plain dict."""

from __future__ import annotations

import difflib
import re
from datetime import datetime
from typing import Any

import numpy as np

from . import glyphs as G
from .badges import Icons, badge_ink, logo_ink
from .layout import (
    DATE_COL,
    DURATION_COL,
    HEADER_Y,
    HERO_Y,
    MATCH_X,
    MATCH_Y,
    NAME_COL,
    NAME_Y,
    ROW_HALF,
    STAT_COLS,
    TOTAL_COL,
    TOTAL_HALF,
    TOTAL_OFFSET,
    WINNER_COL,
    WINNER_Y,
    Layout,
    Row,
    cell,
    find_layout,
    row_background,
)
from .recognizer import Recognizer, snap_hero
from .validate import check, parse_value

TEMPLATE_TRUST = 0.85  # below this confidence a cell is re-read by the recognizer
LOW_RES_SCALE = 0.75  # below this the decimal point nearly vanishes, so trust templates less
LOW_RES_TRUST = 0.94
# No sum rule protects these unless they top their column, so --verify reads them twice.
UNCHECKED_COLS = ("a", "player_dmg", "obj_dmg", "healing")
NUMERIC_FIX = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "Z": "2", "S": "5", "K": "k"})


class Engine:
    """Holds the loaded assets. Create one per worker process."""

    def __init__(self, threads: int = 1, verify: bool = False):
        self.verify = verify
        self.templates = G.Templates.load()
        self.icons = Icons.load()
        self.recognizer = Recognizer(threads=threads)

    def read(self, rgb: np.ndarray) -> dict[str, Any]:
        """Raises NotAScoreboard if the table cannot be found."""
        lay = find_layout(rgb)
        result: dict[str, Any] = _read_header(rgb, lay, self.recognizer)
        teams = []
        for first in (0, 6):
            rows = lay.rows[first : first + 6]
            total_center = rows[-1].bottom + TOTAL_OFFSET * lay.scale
            cut_off = total_center + TOTAL_HALF * lay.scale > rgb.shape[0]
            total = (
                G.Reading("", 0.0)
                if cut_off
                else self._read_stat(rgb, lay, total_center, TOTAL_COL, None, half=TOTAL_HALF)
            )
            logo, logo_score = self.icons.team(logo_ink(rgb, lay, rows[0]))
            teams.append(
                {
                    "name": logo,
                    "logo_score": round(logo_score, 3),
                    "souls_total": total.text,
                    "souls_total_cut_off": cut_off,
                    "souls_total_confidence": round(total.confidence, 3),
                    "souls_total_source": total.source,
                    "players": [self._read_player(rgb, lay, r) for r in rows],
                }
            )
        result["teams"] = teams
        result["winner"], result["winner_raw"] = _read_winner(rgb, lay, teams, self.recognizer)
        everyone = [p for t in teams for p in t["players"]]
        mvp = [p["name"] for p in everyone if p["badge"] == "mvp"]
        result["mvp"] = mvp[0] if len(mvp) == 1 else None
        result["key_players"] = [p["name"] for p in everyone if p["badge"] == "key"]
        result["scale"] = round(lay.scale, 3)
        result["layout"] = {
            "x0": lay.x0,
            "x1": lay.x1,
            "rows": [[r.top, r.bottom] for r in lay.rows],
        }
        result["needs_review"] = check(result)
        return result

    def _read_stat(
        self,
        rgb: np.ndarray,
        lay: Layout,
        center: float,
        col: tuple[int, int],
        bg: np.ndarray | None,
        half: int = ROW_HALF,
    ) -> G.Reading:
        reading = G.read_cell(cell(rgb, lay, center, col, G.ZOOM, half=half), self.templates, bg)
        trust = LOW_RES_TRUST if lay.scale < LOW_RES_SCALE else TEMPLATE_TRUST
        if not reading.burst and not reading.maybe_decimal and reading.confidence >= trust:
            return reading
        # Starburst text and doubtful cells get the slower, stronger reader.
        text, conf = self._recognize_stat(rgb, lay, center, col, half)
        if G.VALUE_RE.match(text) and conf >= reading.confidence:
            return G.Reading(text, conf, reading.burst, "recognizer")
        return reading

    def _recognize_stat(
        self, rgb: np.ndarray, lay: Layout, center: float, col: tuple[int, int], half: int
    ) -> tuple[str, float]:
        y0, y1 = round(center - half * lay.scale), round(center + half * lay.scale)
        text, conf = self.recognizer.read(rgb[y0:y1, round(lay.x(col[0])) : round(lay.x(col[1]))])
        return text.translate(NUMERIC_FIX).replace(" ", ""), conf

    def _read_player(self, rgb: np.ndarray, lay: Layout, row: Row) -> dict[str, Any]:
        bg = row_background(rgb, lay, row)
        name, name_conf = self.recognizer.read(_text_crop(rgb, lay, row, *NAME_Y, bg))
        hero_raw, hero_conf = self.recognizer.read(_text_crop(rgb, lay, row, *HERO_Y, bg))
        hero, similarity = snap_hero(hero_raw)
        badge, badge_score = self.icons.badge(badge_ink(rgb, lay, row))

        player: dict[str, Any] = {
            "name": name,
            "hero": hero,
            "hero_raw": hero_raw,
            "hero_similarity": round(similarity, 3),
            "badge": badge,
            "badge_score": round(badge_score, 3),
            "name_confidence": round(name_conf, 3),
            "hero_confidence": round(hero_conf, 3),
            "cells": {},
            "bursts": [],
            "disagreements": [],
        }
        confidences = [name_conf, hero_conf]
        for key, col in STAT_COLS.items():
            reading = self._read_stat(rgb, lay, row.center, col, bg)
            player[key] = reading.text
            parsed = parse_value(reading.text)
            player[f"{key}_n"] = parsed[0] if parsed else None
            player["cells"][key] = {
                "confidence": round(reading.confidence, 3),
                "source": reading.source,
            }
            confidences.append(reading.confidence)
            if reading.burst:
                player["bursts"].append(key)
            if self.verify and key in UNCHECKED_COLS and not reading.burst:
                second = self._recognize_stat(rgb, lay, row.center, col, ROW_HALF)[0]
                if second != reading.text:
                    player["disagreements"].append(f"{key}: {reading.text} vs {second}")
        player["confidence"] = round(min(confidences), 3)
        return player


def _text_crop(
    rgb: np.ndarray, lay: Layout, row: Row, top: int, bottom: int, bg: np.ndarray
) -> np.ndarray:
    """Native-resolution crop of a line in the name column, trimmed to the text's width."""
    s = lay.scale
    y0, y1 = round(row.center + top * s), round(row.center + bottom * s)
    x0, x1 = round(lay.x(NAME_COL[0])), round(lay.x(NAME_COL[1]))
    crop = rgb[y0:y1, x0:x1]
    on = np.flatnonzero((np.linalg.norm(crop.astype(np.float32) - bg, axis=2) > 40).any(axis=0))
    if on.size == 0:
        return crop
    pad = max(2, round(6 * s))
    return crop[:, max(0, on[0] - pad) : min(crop.shape[1], on[-1] + pad + 1)]


def parse_played_at(raw: str) -> tuple[str | None, bool]:
    """Return (ISO timestamp, day and month could be swapped).

    The format follows the capturing player's locale. Seen so far: `11/6/2026 11:55 PM` and
    `5/11/2026 23:01` (confirmed by the user as 5 November). A 12-hour clock is read as month
    first and a 24-hour clock as day first; when the numbers don't settle it, a 24-hour date is
    reported as ambiguous.
    """
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})\s*(\d{1,2}):(\d{2})\s*([AP]M)?", raw, re.ASCII)
    if not m:
        return None, False
    a, b, year, hour, minute = (int(m[i]) for i in range(1, 6))
    meridiem = m[6]
    if meridiem:
        hour = hour % 12 + (12 if meridiem == "PM" else 0)
    month, day = (a, b) if meridiem else (b, a)
    if month > 12:
        month, day = day, month
    try:
        when = datetime(year, month, day, hour, minute)
    except ValueError:
        return None, False
    return when.strftime("%Y-%m-%dT%H:%M"), not meridiem and a <= 12 and b <= 12 and a != b


def _read_header(rgb: np.ndarray, lay: Layout, rec: Recognizer) -> dict[str, Any]:
    s = lay.scale
    top = lay.rows[0].top
    h, w = rgb.shape[:2]

    def box(x0: float, x1: float, y0: float, y1: float) -> np.ndarray:
        return rgb[max(0, round(y0)) : min(h, round(y1)), max(0, round(x0)) : min(w, round(x1))]

    y0, y1 = top - HEADER_Y[0] * s, top - HEADER_Y[1] * s
    date_raw, date_conf = rec.read(box(lay.x(DATE_COL[0]), lay.x(DATE_COL[1]), y0, y1))
    dur_raw, dur_conf = rec.read(box(lay.x(DURATION_COL[0]), lay.x(DURATION_COL[1]), y0, y1))
    match_box = box(lay.x(MATCH_X), w, top - MATCH_Y[0] * s, top - MATCH_Y[1] * s)
    match_raw, match_conf = rec.read(match_box)

    played_at, ambiguous = parse_played_at(date_raw)

    duration = None
    m = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", dur_raw.replace(" ", ""))
    if m:
        duration = int(m[1] or 0) * 3600 + int(m[2]) * 60 + int(m[3])

    # A match ID whose text runs into the image's right edge has lost digits.
    digits = re.search(r"(\d+)", match_raw)
    truncated = False
    if digits and match_box.size:
        gray = match_box.max(axis=2).astype(np.float32)
        floor = float(np.median(gray))
        edge = float(gray[:, -max(2, round(3 * s)) :].max())
        truncated = edge - floor > 0.4 * (float(gray.max()) - floor)
    return {
        "match_id": int(digits[1]) if digits and not truncated else None,
        "match_id_partial": digits[1] if digits and truncated else None,
        "match_id_truncated": truncated,
        "match_id_raw": match_raw,
        "played_at": played_at,
        "played_at_raw": date_raw,
        "played_at_ambiguous": ambiguous,
        "duration_s": duration,
        "duration_raw": dur_raw,
        "header_confidence": {
            "date": round(date_conf, 3),
            "duration": round(dur_conf, 3),
            "match_id": round(match_conf, 3),
        },
    }


def _read_winner(
    rgb: np.ndarray, lay: Layout, teams: list[dict[str, Any]], rec: Recognizer
) -> tuple[str | None, list[str]]:
    """The winning team has a "Victorious" tag beside its logo."""
    s = lay.scale
    found = []
    raw = []
    for team, first in zip(teams, (lay.rows[0], lay.rows[6]), strict=True):
        y0, y1 = round(first.top - WINNER_Y[0] * s), round(first.top - WINNER_Y[1] * s)
        text, _ = rec.read(rgb[y0:y1, round(lay.x(WINNER_COL[0])) : round(lay.x(WINNER_COL[1]))])
        raw.append(text)
        ratio = difflib.SequenceMatcher(None, text.lower(), "victorious").ratio()
        if ratio >= 0.7:
            found.append(team["name"])
    return (found[0] if len(found) == 1 else None), raw
