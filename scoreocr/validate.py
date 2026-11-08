"""Consistency checks. The scoreboard repeats itself enough to catch most misreads."""

from __future__ import annotations

import re
from typing import Any

MIN_FIELD_CONFIDENCE = 0.6
MIN_SCALE = 0.6  # below this the `k` suffix and decimal points blur away (seen at 1024 px wide)
MIN_HERO_SIMILARITY = 0.7
NAME_WARN_CONFIDENCE = 0.85  # real misreads measured 0.81 and 0.84, correct names 0.90 or more
COMPACT_COLS = ("souls", "player_dmg", "obj_dmg", "healing")
COUNT_COLS = ("k", "d", "a")
BURST_COLS = ("souls", "k", "a", "player_dmg", "obj_dmg", "healing")


def parse_value(text: str | None) -> tuple[int, int] | None:
    """Parse a displayed value into (number, rounding tolerance). None if malformed."""
    if not text:
        return None
    if re.fullmatch(r"\d{1,3}", text, re.ASCII):
        return int(text), 0
    if re.fullmatch(r"\d{1,3}k", text, re.ASCII):
        return int(text[:-1]) * 1000, 500
    if re.fullmatch(r"\d\.\dk", text, re.ASCII):
        return round(float(text[:-1]) * 1000), 50
    return None


def check(result: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems. Empty means every check passed."""
    problems: list[str] = []
    if result["scale"] < MIN_SCALE:
        problems.append(f"low resolution (table scale {result['scale']})")
    teams = result["teams"]
    players = [(t["name"], p) for t in teams for p in t["players"]]

    for _, p in players:
        who = p["name"] or "?"
        if not p["name"]:
            problems.append("player name unread")
        if p["hero"] is None or p["hero_similarity"] < MIN_HERO_SIMILARITY:
            problems.append(f"{who}: hero not on roster ({p['hero_raw']!r})")
        for col in COMPACT_COLS:
            if parse_value(p[col]) is None:
                problems.append(f"{who}: unreadable {col} ({p[col]!r})")
        for col in COUNT_COLS:
            if not re.fullmatch(r"\d{1,3}", p[col] or "", re.ASCII):
                problems.append(f"{who}: unreadable {col} ({p[col]!r})")
        for disagreement in p.get("disagreements", []):
            problems.append(f"{who}: readers disagree on {disagreement}")
        name_conf = min(p["name_confidence"], p["hero_confidence"])
        if name_conf < MIN_FIELD_CONFIDENCE:
            problems.append(f"{who}: low confidence in name or hero ({name_conf:.2f})")
        elif p["name_confidence"] < NAME_WARN_CONFIDENCE:
            problems.append(f"soft: {who}: name may be misread ({p['name_confidence']:.2f})")
        stat_conf = min(c["confidence"] for c in p["cells"].values())
        if stat_conf < MIN_FIELD_CONFIDENCE:
            problems.append(f"{who}: low confidence in a stat ({stat_conf:.2f})")

    # Team souls must add up to the printed total, within the game's rounding.
    for t in teams:
        total = parse_value(t["souls_total"])
        values = [parse_value(p["souls"]) for p in t["players"]]
        if t.get("souls_total_cut_off"):
            problems.append(f"soft: {t['name']}: souls total is cut off, so the sum is unchecked")
        elif total is None:
            problems.append(f"{t['name']}: unreadable souls total ({t['souls_total']!r})")
        elif all(values):
            spread = sum(v[1] for v in values) + total[1]
            if abs(sum(v[0] for v in values) - total[0]) > spread:
                problems.append(f"{t['name']}: souls do not sum to {t['souls_total']}")

    # Kills on one team should equal deaths on the other. Soft: non-player deaths can break it.
    if len(teams) == 2:
        for a, b in ((teams[0], teams[1]), (teams[1], teams[0])):
            kills = [parse_value(p["k"]) for p in a["players"]]
            deaths = [parse_value(p["d"]) for p in b["players"]]
            if all(kills) and all(deaths):
                k_sum, d_sum = sum(v[0] for v in kills), sum(v[0] for v in deaths)
                if k_sum != d_sum:
                    problems.append(
                        f"soft: {a['name']} kills {k_sum} != {b['name']} deaths {d_sum}"
                    )

    # A starburst marks the highest value in its column.
    for col in BURST_COLS:
        values = [(parse_value(p[col]), p) for _, p in players]
        known = [v[0][0] for v in values if v[0]]
        if not known:
            continue
        top = max(known)
        leader = next(p for parsed, p in values if parsed and parsed[0] == top)
        for parsed, p in values:
            if col in p["bursts"] and parsed and parsed[0] != top:
                problems.append(
                    f"{p['name']}: starred {col} {p[col]} is not the column maximum "
                    f"({leader['name']} was read as {leader[col]}; one of them is wrong)"
                )

    badges = [p["badge"] for _, p in players]
    if badges.count("mvp") != 1:
        problems.append(f"expected 1 MVP, found {badges.count('mvp')}")
    if badges.count("key") != 2:
        problems.append(f"expected 2 key players, found {badges.count('key')}")

    if result["winner"] is None:
        problems.append("winner not found")
    if any(t["name"] is None for t in teams):
        problems.append("team logo not recognised")
    if result["played_at"] is None:
        problems.append(f"date unread ({result['played_at_raw']!r})")
    elif result["played_at_ambiguous"]:
        problems.append(
            f"soft: day and month could be swapped in {result['played_at_raw']!r}, "
            f"assumed {result['played_at']}"
        )
    if result["duration_s"] is None:
        problems.append(f"duration unread ({result['duration_raw']!r})")
    return problems
