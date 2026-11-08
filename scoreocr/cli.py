"""Batch CLI.

    python -m scoreocr survey <folder> [--out <folder>]
    python -m scoreocr run <folder> [--out <folder>] [--workers N] [--limit N] [--verify]
                           [--reports all|flagged|none]

Everything is written under --out (default scoreocr_out):

    survey.txt, survey.csv      what `survey` found
    progress.md                 start here: totals per phase, and which images to open
    results.jsonl, players.csv  all data
    images/<name>_<hash>/       per image: progress.md, overlay.webp and result.json

`run` skips images it has already read (by SHA-256), so a large run can stop and restart.
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import multiprocessing as mp
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .layout import STAT_COLS, NotAScoreboard, find_layout
from .report import phase_statuses, report_folder, write_image_report, write_run_progress

EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
CSV_FIELDS = [
    "image", "status", "match_id", "played_at", "duration_s", "winner", "team", "won",
    "name", "hero", "badge",
    *(f for k in STAT_COLS for f in (k, f"{k}_n")),
    "confidence",
]  # fmt: skip

_engine: Any = None
_out: Path = Path()
_reports = "all"
_done: set[str] = set()


def iter_images(folder: Path) -> Iterator[Path]:
    for path in sorted(folder.rglob("*")):
        if path.suffix.lower() in EXTENSIONS and path.is_file():
            yield path


def _load(path: Path) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"))


def _init_worker(out: str, verify: bool, reports: str, done: set[str]) -> None:
    from .engine import Engine

    global _engine, _out, _reports, _done
    _engine = Engine(verify=verify)
    _out = Path(out)
    _reports = reports
    _done = done


def _process(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if sha in _done:
        return {"image": str(path), "sha256": sha, "status": "skipped"}
    record: dict[str, Any] = {"image": str(path), "sha256": sha}
    rgb = None
    try:
        rgb = _load(path)
        result = _engine.read(rgb)
        hard = [p for p in result["needs_review"] if not p.startswith("soft:")]
        record |= {"status": "review" if hard else "ok", **result}
    except NotAScoreboard as e:
        record |= {"status": "rejected", "reason": str(e)}
    except Exception as e:  # one bad file must not stop a long batch
        record |= {"status": "error", "reason": f"{type(e).__name__}: {e}"}
    record["phases"] = phase_statuses(record)
    if _reports == "all" or (_reports == "flagged" and record["status"] != "ok"):
        folder = report_folder(path, sha)
        record["report"] = folder
        write_image_report(_out / folder, record, rgb)
    return record


def survey(folder: Path, out: Path) -> int:
    sizes: collections.Counter[tuple[int, int]] = collections.Counter()
    reasons: collections.Counter[str] = collections.Counter()
    found = 0
    examples: dict[str, str] = {}
    paths = list(iter_images(folder))
    out.mkdir(parents=True, exist_ok=True)
    with (out / "survey.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["image", "width", "height", "table_found", "scale", "reason"])
        for path in paths:
            width = height = scale = ""
            try:
                rgb = _load(path)
                height, width = rgb.shape[:2]
                sizes[(width, height)] += 1
                scale = f"{find_layout(rgb).scale:.3f}"
                found += 1
                reason = ""
            except NotAScoreboard as e:
                reason = str(e)
            except Exception as e:
                reason = f"unreadable: {type(e).__name__}"
            if reason:
                reasons[reason] += 1
                examples.setdefault(reason, str(path))
            writer.writerow([path, width, height, bool(scale), scale, reason])
    lines = [f"{len(paths)} images, table found in {found}"]
    lines += [f"  {w}x{h}: {n}" for (w, h), n in sizes.most_common(30)]
    lines += [
        f"  rejected, {reason}: {n} (e.g. {examples[reason]})"
        for reason, n in reasons.most_common()
    ]
    text = "\n".join(lines)
    print(text)
    (out / "survey.txt").write_text(text + "\n", encoding="utf-8")
    print(f"saved to {out / 'survey.txt'} and {out / 'survey.csv'}")
    return 0


def _load_done(results: Path) -> set[str]:
    if not results.exists():
        return set()
    with results.open(encoding="utf-8") as f:
        return {json.loads(line)["sha256"] for line in f if line.strip()}


def write_csv(results: Path, out: Path) -> None:
    with results.open(encoding="utf-8") as f, out.open("w", newline="", encoding="utf-8-sig") as o:
        writer = csv.DictWriter(o, CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for line in f:
            rec = json.loads(line)
            for team in rec.get("teams", []):
                won = team["name"] == rec["winner"] if rec["winner"] else None
                for player in team["players"]:
                    writer.writerow({**rec, **player, "team": team["name"], "won": won})


def run(
    folder: Path,
    out: Path,
    workers: int,
    limit: int | None,
    verify: bool = False,
    reports: str = "all",
) -> int:
    out.mkdir(parents=True, exist_ok=True)
    results = out / "results.jsonl"
    done = _load_done(results)
    paths = [str(p) for p in iter_images(folder)]
    if limit:
        paths = paths[:limit]

    counts: collections.Counter[str] = collections.Counter()
    started = time.time()
    with (
        mp.Pool(workers, _init_worker, (str(out), verify, reports, done)) as pool,
        results.open("a", encoding="utf-8") as f,
    ):
        for i, rec in enumerate(pool.imap_unordered(_process, paths, chunksize=4), 1):
            # A second copy of an image in this same run reaches here as a full record.
            if rec["status"] == "skipped" or rec["sha256"] in done:
                counts["duplicate or already read"] += 1
            else:
                done.add(rec["sha256"])
                counts[rec["status"]] += 1
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
            if i % 100 == 0:
                print(f"{i}/{len(paths)} {dict(counts)}", flush=True)
                write_run_progress(results, out)
    write_csv(results, out / "players.csv")
    write_run_progress(results, out)
    elapsed = time.time() - started
    print(f"{len(paths)} images in {elapsed:.1f}s with {workers} workers: {dict(counts)}")
    print(f"Start with {out / 'progress.md'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scoreocr", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_survey = sub.add_parser("survey", help="report image sizes and whether the table is found")
    p_survey.add_argument("folder", type=Path)
    p_survey.add_argument("--out", type=Path, default=Path("scoreocr_out"))
    p_run = sub.add_parser("run", help="read every image into JSONL and CSV")
    p_run.add_argument("folder", type=Path)
    p_run.add_argument("--out", type=Path, default=Path("scoreocr_out"))
    p_run.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    p_run.add_argument("--limit", type=int)
    p_run.add_argument(
        "--verify",
        action="store_true",
        help="read assists, damage and healing twice and flag disagreements; slower",
    )
    p_run.add_argument(
        "--reports",
        choices=["all", "flagged", "none"],
        default="all",
        help="which images get a review folder; 'flagged' saves disk on very large sets",
    )
    args = parser.parse_args(argv)
    if not args.folder.is_dir():
        parser.error(f"not a folder: {args.folder}")
    if args.command == "survey":
        return survey(args.folder, args.out)
    return run(args.folder, args.out, args.workers, args.limit, args.verify, args.reports)
