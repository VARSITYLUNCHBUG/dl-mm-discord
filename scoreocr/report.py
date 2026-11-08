"""Per-image and per-run review files, organised by the phases of a read."""

from __future__ import annotations

import collections
import json
import re
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import layout as L
from .badges import BADGE_HALF, LOGO_ABOVE, LOGO_COL
from .validate import parse_value

PHASES = {
    "layout": "1. Layout",
    "header": "2. Header",
    "teams": "3. Teams and winner",
    "stats": "4. Stats",
    "names": "5. Names and heroes",
    "badges": "6. Badges",
}
# Which phase each validation message belongs to. These match the whole message because player
# names appear in messages, and a player can be called anything, including "MVP". Anything
# unmatched is listed under "stats", the phase with the most checks, rather than being lost.
_PROBLEM_PHASE = tuple(
    (re.compile(pattern), phase)
    for pattern, phase in (
        (r"low resolution \(.*\)", "layout"),
        (r"date unread \(.*\)", "header"),
        (r"soft: day and month could be swapped in .*", "header"),
        (r"duration unread \(.*\)", "header"),
        (r"winner not found", "teams"),
        (r"team logo not recognised", "teams"),
        (r"player name unread", "names"),
        (r".*: hero not on roster \(.*\)", "names"),
        (r".*: low confidence in name or hero \([\d.]+\)", "names"),
        (r"soft: .*: name may be misread \([\d.]+\)", "names"),
        (r"expected \d+ MVP, found \d+", "badges"),
        (r"expected \d+ key players, found \d+", "badges"),
    )
)
LOW_CONFIDENCE = 0.85  # marked `?` in reports: worth a look, not necessarily wrong
MAX_LISTED_IMAGES = 500

_GOOD = (90, 220, 120)
_SECOND_READER = (90, 200, 255)
_BAD = (255, 80, 80)
_LABEL = (255, 225, 90)


def phase_of(problem: str) -> str:
    for pattern, phase in _PROBLEM_PHASE:
        if pattern.fullmatch(problem):
            return phase
    return "stats"


def phase_statuses(record: dict[str, Any]) -> dict[str, str]:
    if record["status"] in ("rejected", "error"):
        failed = {"layout": "failed"} if record["status"] == "rejected" else {"layout": "error"}
        return {p: failed.get(p, "not run") for p in PHASES}
    statuses = dict.fromkeys(PHASES, "ok")
    for problem in record["needs_review"]:
        phase = phase_of(problem)
        if not problem.startswith("soft:"):
            statuses[phase] = "needs review"
        elif statuses[phase] == "ok":
            statuses[phase] = "warning"
    return statuses


def report_folder(path: Path, sha: str) -> str:
    return f"images/{path.stem}_{sha[:8]}"


def _esc(value: Any) -> str:
    return "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join(_esc(v) for v in row) + " |" for row in rows]
    return [*lines, ""]


def _mark_cell(player: dict[str, Any], key: str) -> str:
    info = player["cells"][key]
    text = player[key] or "(unread)"
    if key in player["bursts"]:
        text = f"★{text}"
    if info["source"] == "recognizer":
        text += "*"
    if info["confidence"] < LOW_CONFIDENCE:
        text += f" ? {info['confidence']:.2f}"
    return text


def image_progress(record: dict[str, Any], has_overlay: bool) -> str:
    """The per-image review file, one section per phase."""
    statuses = record["phases"]
    out = [f"# {Path(record['image']).name}", "", f"- **Status:** {record['status']}"]
    out.append(f"- **Source:** `{record['image']}`")
    out.append(f"- **SHA-256:** `{record['sha256']}`")
    if has_overlay:
        out.append("- **Check by eye:** [overlay.webp](overlay.webp) draws every box and reading")
    out.append("- **Full data:** [result.json](result.json)")
    out.append("")

    def section(phase: str) -> None:
        out.extend([f"## {PHASES[phase]}: {statuses[phase]}", ""])
        for problem in record.get("needs_review", []):
            if phase_of(problem) == phase:
                out.append(
                    f"- **{'Warning' if problem.startswith('soft:') else 'Problem'}:** "
                    f"{problem.removeprefix('soft: ')}"
                )
        if out[-1] != "":
            out.append("")

    if record["status"] in ("rejected", "error"):
        section("layout")
        out.extend([f"Stopped here: {record.get('reason')}", ""])
        return "\n".join(out)

    lay = record["layout"]
    section("layout")
    out.extend(
        [
            f"Table found at x {lay['x0']:.0f} to {lay['x1']:.0f}, scale {record['scale']}, "
            f"{len(lay['rows'])} rows.",
            "",
        ]
    )

    section("header")
    conf = record["header_confidence"]
    out.extend(
        _table(
            ["Field", "Read as", "Parsed", "Confidence"],
            [
                ["Date", record["played_at_raw"], record["played_at"], f"{conf['date']:.2f}"],
                ["Duration", record["duration_raw"], record["duration_s"],
                 f"{conf['duration']:.2f}"],
                ["Match ID", record["match_id_raw"],
                 record["match_id"] or f"{record['match_id_partial']} (cut off)",
                 f"{conf['match_id']:.2f}"],
            ],
        )
    )  # fmt: skip

    section("teams")
    rows = [
        [t["name"], t["logo_score"], raw, "yes" if t["name"] == record["winner"] else ""]
        for t, raw in zip(record["teams"], record["winner_raw"], strict=True)
    ]
    out.extend(_table(["Team (slot order)", "Logo match", "Tag read as", "Winner"], rows))

    section("stats")
    out.extend(["`*` second reader used, `?` low confidence, `★` starburst.", ""])
    players = [p for t in record["teams"] for p in t["players"]]
    rows = [[p["name"], *(_mark_cell(p, k) for k in L.STAT_COLS)] for p in players]
    out.extend(_table(["Player", *L.STAT_COLS], rows))
    for t in record["teams"]:
        total = parse_value(t["souls_total"])
        values = [parse_value(p["souls"]) for p in t["players"]]
        summed = sum(v[0] for v in values if v)
        kills = sum(v[0] for v in (parse_value(p["k"]) for p in t["players"]) if v)
        deaths = sum(v[0] for v in (parse_value(p["d"]) for p in t["players"]) if v)
        out.append(
            f"- **{t['name']}:** souls total {t['souls_total']} "
            f"(players add to {summed / 1000:.0f}k), kills {kills}, deaths {deaths}"
            + ("" if total else " (total cut off)" if t["souls_total_cut_off"] else " (unreadable)")
        )
    out.append("")

    section("names")
    rows = [
        [p["name"], f"{p['name_confidence']:.2f}", p["hero_raw"], p["hero"], p["hero_similarity"],
         f"{p['hero_confidence']:.2f}"]
        for p in players
    ]  # fmt: skip
    out.extend(
        _table(["Name", "Confidence", "Hero read as", "Hero", "Roster match", "Confidence"], rows)
    )

    section("badges")
    out.extend(
        [
            f"- **MVP:** {record['mvp']}",
            f"- **Key players:** {', '.join(record['key_players']) or 'none'}",
            "",
        ]
    )
    rows = [[p["name"], p["badge"], p["badge_score"]] for p in players if p["badge"]]
    out.extend(_table(["Player", "Badge", "Icon match"], rows))
    return "\n".join(out)


def draw_overlay(rgb: np.ndarray, record: dict[str, Any]) -> Image.Image:
    """Copy of the screenshot with each read box and what was read in it."""
    lay = L.Layout(
        rows=tuple(L.Row(a, b) for a, b in record["layout"]["rows"]),
        x0=record["layout"]["x0"],
        x1=record["layout"]["x1"],
    )
    s = lay.scale
    image = Image.fromarray(rgb)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=max(10, round(13 * s)))

    def box(x0: float, x1: float, y0: float, y1: float, colour: tuple[int, int, int]) -> None:
        draw.rectangle((lay.x(x0), y0, lay.x(x1), y1), outline=colour)

    def label(x: float, y: float, text: str, colour: tuple[int, int, int] = _LABEL) -> None:
        draw.text((x, y), text, font=font, fill=colour, stroke_width=2, stroke_fill=(0, 0, 0))

    def cell_colour(text: str | None, info: dict[str, Any]) -> tuple[int, int, int]:
        if parse_value(text) is None or info["confidence"] < LOW_CONFIDENCE:
            return _BAD
        return _SECOND_READER if info["source"] == "recognizer" else _GOOD

    top = lay.rows[0].top
    y0, y1 = top - L.HEADER_Y[0] * s, top - L.HEADER_Y[1] * s
    for col, text in (
        (L.DATE_COL, str(record["played_at"])),
        (L.DURATION_COL, str(record["duration_s"])),
    ):
        box(*col, y0, y1, _LABEL if text != "None" else _BAD)
        label(lay.x(col[0]), y0 - 15 * s, text)
    y0, y1 = top - L.MATCH_Y[0] * s, top - L.MATCH_Y[1] * s
    draw.rectangle((lay.x(L.MATCH_X), y0, rgb.shape[1] - 1, y1), outline=_LABEL)
    label(lay.x(L.MATCH_X), y1 + 2, f"id {record['match_id'] or record['match_id_partial']}")

    players = iter(p for t in record["teams"] for p in t["players"])
    for team, first in zip(record["teams"], (0, 6), strict=True):
        rows = lay.rows[first : first + 6]
        above, below = LOGO_ABOVE
        box(*LOGO_COL, rows[0].top - above * s, rows[0].top - below * s, _LABEL)
        winner = " WINNER" if team["name"] == record["winner"] else ""
        label(lay.x(LOGO_COL[0]), rows[0].top - (above + 14) * s, f"{team['name']}{winner}")

        center = rows[-1].bottom + L.TOTAL_OFFSET * s
        info = {"confidence": team["souls_total_confidence"]}
        info["source"] = team["souls_total_source"]
        box(*L.TOTAL_COL, center - L.TOTAL_HALF * s, center + L.TOTAL_HALF * s,
            cell_colour(team["souls_total"], info))  # fmt: skip
        label(lay.x(L.TOTAL_COL[1]) + 4, center - 8 * s, str(team["souls_total"]))

        for row in rows:
            p = next(players)
            c = row.center
            box(*L.NAME_COL, c + L.NAME_Y[0] * s, c + L.HERO_Y[1] * s, _LABEL)
            for key, col in L.STAT_COLS.items():
                y0, y1 = c - L.ROW_HALF * s, c + L.ROW_HALF * s
                colour = cell_colour(p[key], p["cells"][key])
                box(*col, y0, y1, colour)
                label(lay.x(col[0]), y0 - 13 * s, str(p[key]), colour)
            if p["badge"]:
                box(*L.BADGE_COL, c - BADGE_HALF * s, c + BADGE_HALF * s, _LABEL)
            read = f"{p['name']} / {p['hero']}"
            if p["badge"]:
                read += f" / {p['badge'].upper()}"
            label(lay.x(1094), c - 9 * s, read)
    return image


def write_image_report(folder: Path, record: dict[str, Any], rgb: np.ndarray | None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    overlay = rgb is not None and "layout" in record
    if overlay:
        draw_overlay(rgb, record).save(folder / "overlay.webp", quality=85)
    (folder / "result.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (folder / "progress.md").write_text(image_progress(record, overlay), encoding="utf-8")


def write_run_progress(results: Path, out: Path) -> None:
    """The run-level review file: totals per phase, then which images to open."""
    records = []
    with results.open(encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
    status_counts = collections.Counter(r["status"] for r in records)
    per_phase = {p: collections.Counter() for p in PHASES}
    for r in records:
        for phase, status in r["phases"].items():
            per_phase[phase][status] += 1

    lines = ["# OCR run progress", "", f"**{len(records)} images read:** "]
    lines[-1] += ", ".join(f"{n} {s}" for s, n in status_counts.most_common()) or "none"
    lines.append("")
    survey = out / "survey.txt"
    if survey.exists():
        lines.extend(["Before this run: [survey.txt](survey.txt), [survey.csv](survey.csv)", ""])
    lines.extend(["## By phase", ""])
    columns = ["ok", "warning", "needs review", "failed", "error", "not run"]
    rows = [[title, *(per_phase[p][c] for c in columns)] for p, title in PHASES.items()]
    lines.extend(_table(["Phase", *columns], rows))

    def image_rows(chosen: list[dict[str, Any]]) -> list[str]:
        rows = []
        for r in chosen:
            name = Path(r["image"]).name
            link = f"[{name}]({r['report']}/progress.md)" if r.get("report") else name
            rows.append([link, r["status"], *(r["phases"][p] for p in PHASES)])
        return _table(["Image", "Status", *PHASES], rows)

    attention = [r for r in records if r["status"] != "ok"]
    lines.extend([f"## Needs attention ({len(attention)})", ""])
    lines.extend(image_rows(attention) if attention else ["None.", ""])
    if len(records) <= MAX_LISTED_IMAGES:
        lines.extend(["## All images", "", *image_rows(records)])
    else:
        lines.append(f"Over {MAX_LISTED_IMAGES} images: see results.jsonl for the full list.")
    (out / "progress.md").write_text("\n".join(lines), encoding="utf-8")
