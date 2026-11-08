"""Run with: uv run --group ocr pytest tests/scoreocr -p no:django --noconftest"""

from __future__ import annotations

import copy
import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
Image = pytest.importorskip("PIL.Image")

from scoreocr import glyphs as G  # noqa: E402
from scoreocr.cli import main  # noqa: E402
from scoreocr.engine import Engine, parse_played_at  # noqa: E402
from scoreocr.layout import STAT_COLS, NotAScoreboard  # noqa: E402
from scoreocr.report import phase_of  # noqa: E402
from scoreocr.validate import check, parse_value  # noqa: E402

FIXTURES = Path(__file__).parent.parent / "fixtures"
SAMPLE = FIXTURES / "scoreboard_sample.webp"
LABELS = json.loads((FIXTURES / "scoreboard_sample.json").read_text(encoding="utf-8"))
SAMPLE_W, SAMPLE_H = Image.open(SAMPLE).size


@pytest.fixture(scope="module")
def engine() -> Engine:
    return Engine()


@pytest.fixture(scope="module")
def sample_result(engine: Engine) -> dict:
    return engine.read(np.asarray(Image.open(SAMPLE).convert("RGB")))


def resized(width: int) -> np.ndarray:
    size = (width, round(SAMPLE_H * width / SAMPLE_W))
    return np.asarray(Image.open(SAMPLE).convert("RGB").resize(size, Image.LANCZOS))


def differences(result: dict, labels: dict = LABELS) -> list[tuple]:
    diffs = []
    for key in ("played_at", "duration_s", "winner", "mvp", "key_players", "match_id_truncated"):
        if result[key] != labels[key]:
            diffs.append((key, result[key], labels[key]))
    for got_team, want_team in zip(result["teams"], labels["teams"], strict=True):
        for key in ("name", "souls_total"):
            if got_team[key] != want_team[key]:
                diffs.append((key, got_team[key], want_team[key]))
        for got, want in zip(got_team["players"], want_team["players"], strict=True):
            for key in ("name", "hero", "badge", *STAT_COLS):
                if got[key] != want[key]:
                    diffs.append((want["name"], key, got[key], want[key]))
    return diffs


def test_golden_sample(sample_result: dict) -> None:
    assert differences(sample_result) == []
    assert sample_result["needs_review"] == []
    assert sample_result["match_id"] is None
    assert sample_result["match_id_partial"] == "132533"


def test_real_screenshot_not_used_for_templates(engine: Engine) -> None:
    labels = json.loads((FIXTURES / "scoreboard_real_1.json").read_text(encoding="utf-8"))
    rgb = np.asarray(Image.open(FIXTURES / "scoreboard_real_1.png").convert("RGB"))
    result = engine.read(rgb)
    assert differences(result, labels) == []
    assert result["match_id"] == labels["match_id"]
    # Both are real: two deaths had no player kill, and 5/11 can be read either way round.
    assert all(p.startswith("soft:") for p in result["needs_review"]), result["needs_review"]
    assert len(result["needs_review"]) == 2


def test_second_real_screenshot_with_known_name_limits(engine: Engine) -> None:
    labels = json.loads((FIXTURES / "scoreboard_real_2.json").read_text(encoding="utf-8"))
    rgb = np.asarray(Image.open(FIXTURES / "scoreboard_real_2.png").convert("RGB"))
    result = engine.read(rgb)
    # Two names are genuinely misread. The model has no Greek letters, and K/k look alike here.
    # What matters is that neither passes silently.
    assert differences(result, labels) == [
        ("BONK!?", "name", "BONk!?", "BONK!?"),
        ("SN\u039eK", "name", "SN=K", "SN\u039eK"),
    ]
    review = result["needs_review"]
    assert all(p.startswith("soft:") for p in review), review
    assert any("BONk!?: name may be misread" in p for p in review)
    assert any("SN=K: name may be misread" in p for p in review)
    assert any("Archmother: souls total is cut off" in p for p in review)


def test_missed_decimal_point_is_recovered_by_the_second_reader(engine: Engine) -> None:
    """Regression: the templates read this 7.1k as 71k because the dot was faint."""
    rgb = np.asarray(Image.open(FIXTURES / "scoreboard_real_2.png").convert("RGB"))
    fent = engine.read(rgb)["teams"][1]["players"][4]
    assert fent["obj_dmg"] == "7.1k"
    assert fent["cells"]["obj_dmg"]["source"] == "recognizer"


def test_starburst_problem_names_both_players(sample_result: dict) -> None:
    problems = corrupt(sample_result, 0, 0, "obj_dmg", "71k")
    message = next(p for p in problems if "not the column maximum" in p)
    assert message.startswith("CITIGAW: starred obj_dmg 26k")
    assert "daxter was read as 71k" in message


def test_total_below_the_image_is_a_warning_not_a_misread(engine: Engine) -> None:
    rgb = np.asarray(Image.open(SAMPLE).convert("RGB"))[:1040]
    result = engine.read(rgb)
    assert result["teams"][1]["souls_total_cut_off"] is True
    assert result["teams"][0]["souls_total"] == "348k"
    assert [p for p in result["needs_review"] if "souls total" in p] == [
        "soft: Archmother: souls total is cut off, so the sum is unchecked"
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("11/6/2026 11:55 PM", ("2026-11-06T23:55", False)),
        ("11/6/202611:55PM", ("2026-11-06T23:55", False)),
        ("12/6/2026 12:05 AM", ("2026-12-06T00:05", False)),
        ("5/11/2026 23:01", ("2026-11-05T23:01", True)),
        ("5/11/202623:01", ("2026-11-05T23:01", True)),
        ("25/11/2026 23:01", ("2026-11-25T23:01", False)),
        ("5/5/2026 23:01", ("2026-05-05T23:01", False)),
        ("garbage", (None, False)),
        ("13/13/2026 23:01", (None, False)),
    ],
)
def test_date_formats(raw: str, expected: tuple) -> None:
    assert parse_played_at(raw) == expected


@pytest.mark.parametrize("width", [1280, 1600, 1920, 2560, 3840])
def test_same_output_at_other_resolutions(engine: Engine, width: int) -> None:
    result = engine.read(resized(width))
    assert differences(result) == []
    assert result["needs_review"] == []


def test_very_small_image_is_flagged_not_trusted(engine: Engine) -> None:
    assert any("low resolution" in p for p in engine.read(resized(1024))["needs_review"])


def test_complete_match_id_is_kept_when_text_stops_before_the_edge(engine: Engine) -> None:
    rgb = np.asarray(Image.open(SAMPLE).convert("RGB"))
    padded = np.zeros((SAMPLE_H, SAMPLE_W + 200, 3), dtype=np.uint8)
    padded[:, :SAMPLE_W] = rgb
    result = engine.read(padded)
    assert result["match_id_truncated"] is False
    assert result["match_id"] == 132533


def test_blank_image_is_rejected(engine: Engine) -> None:
    with pytest.raises(NotAScoreboard):
        engine.read(np.zeros((1080, 1920, 3), dtype=np.uint8))


def corrupt(result: dict, team: int, player: int, key: str, value: str) -> list[str]:
    bad = copy.deepcopy(result)
    bad["teams"][team]["players"][player][key] = value
    return check(bad)


@pytest.mark.parametrize(
    ("team", "player", "key", "value", "expected"),
    [
        (0, 0, "souls", "99k", "do not sum"),
        (0, 0, "k", "1", "kills"),
        (1, 2, "d", "9", "kills"),
        (0, 4, "souls", "11k", "not the column maximum"),
        (1, 3, "player_dmg", "13k", "not the column maximum"),
        (0, 0, "badge", "mvp", "MVP"),
        (0, 4, "badge", None, "key players"),
        (0, 0, "obj_dmg", "2.3", "unreadable obj_dmg"),
        (0, 0, "hero", None, "hero"),
        (0, 0, "a", "14k", "unreadable a"),
    ],
)
def test_validators_catch_a_single_corrupted_field(
    sample_result: dict, team: int, player: int, key: str, value: str | None, expected: str
) -> None:
    problems = corrupt(sample_result, team, player, key, value)
    assert any(expected in p for p in problems), problems


def test_plausible_damage_misread_passes_without_verify(sample_result: dict) -> None:
    """Documents the gap --verify exists for: no rule protects a non-maximum damage value."""
    assert corrupt(sample_result, 0, 0, "obj_dmg", "2.8k") == []


def test_verify_is_clean_on_the_sample_and_flags_a_disagreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifying = Engine(verify=True)
    rgb = np.asarray(Image.open(SAMPLE).convert("RGB"))
    clean = verifying.read(rgb)
    assert differences(clean) == []
    assert clean["needs_review"] == []

    real = G.read_cell

    def misread_three(crop, tpl, bg=None):
        reading = real(crop, tpl, bg)
        return G.Reading(reading.text.replace("3", "8"), reading.confidence, reading.burst)

    monkeypatch.setattr(G, "read_cell", misread_three)
    assert any("readers disagree" in p for p in verifying.read(rgb)["needs_review"])


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("4", (4, 0)),
        ("49k", (49000, 500)),
        ("113k", (113000, 500)),
        ("2.3k", (2300, 50)),
        ("", None),
        (None, None),
        ("23.4k", None),
    ],
)
def test_parse_value(text: str | None, parsed: tuple[int, int] | None) -> None:
    assert parse_value(text) == parsed


def test_cli_end_to_end_and_resume(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    images = tmp_path / "in"
    images.mkdir()
    Image.open(SAMPLE).convert("RGB").save(images / "a.png")
    Image.fromarray(resized(2560)).save(images / "b.png")
    Image.fromarray(np.zeros((1080, 1920, 3), dtype=np.uint8)).save(images / "junk.png")
    (images / "broken.png").write_bytes(b"not an image")
    out = tmp_path / "out"

    assert main(["survey", str(images), "--out", str(out)]) == 0
    assert "table found in 2" in capsys.readouterr().out

    assert main(["run", str(images), "--out", str(out), "--workers", "2"]) == 0
    records = [json.loads(x) for x in (out / "results.jsonl").read_text("utf-8").splitlines()]
    assert sorted(r["status"] for r in records) == ["error", "ok", "ok", "rejected"]
    with (out / "players.csv").open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 24
    assert {r["name"] for r in rows if r["badge"] == "mvp"} == {"CITIGAW"}

    assert (out / "survey.txt").read_text("utf-8").startswith("4 images, table found in 2")
    with (out / "survey.csv").open(encoding="utf-8-sig") as f:
        assert len(list(csv.DictReader(f))) == 4

    progress = (out / "progress.md").read_text("utf-8")
    assert "**4 images read:**" in progress
    assert "1. Layout" in progress
    assert "survey.txt" in progress
    ok = next(r for r in records if r["status"] == "ok")
    folder = out / ok["report"]
    page = (folder / "progress.md").read_text("utf-8")
    for title in ("1. Layout", "2. Header", "3. Teams", "4. Stats", "5. Names", "6. Badges"):
        assert f"## {title}" in page
    assert "CITIGAW" in page
    assert Image.open(folder / "overlay.webp").size[0] > 0
    assert json.loads((folder / "result.json").read_text("utf-8"))["mvp"] == "CITIGAW"
    rejected = next(r for r in records if r["status"] == "rejected")
    page = (out / rejected["report"] / "progress.md").read_text("utf-8")
    assert "Stopped here" in page
    assert not (out / rejected["report"] / "overlay.webp").exists()
    assert rejected["phases"]["layout"] == "failed"
    assert rejected["phases"]["stats"] == "not run"

    capsys.readouterr()
    assert main(["run", str(images), "--out", str(out), "--workers", "2"]) == 0
    assert "'duplicate or already read': 4" in capsys.readouterr().out
    assert len((out / "results.jsonl").read_text("utf-8").splitlines()) == 4


def test_real_command_starts_worker_processes(tmp_path: Path) -> None:
    """Calling main() directly hides failures that only occur under `python -m` with spawn."""
    images = tmp_path / "in"
    images.mkdir()
    Image.open(SAMPLE).convert("RGB").save(images / "a.png")
    out = tmp_path / "out"
    done = subprocess.run(
        [sys.executable, "-m", "scoreocr", "run", str(images), "--out", str(out), "--workers", "2"],
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
        cwd=Path(__file__).parent.parent.parent,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads((out / "results.jsonl").read_text("utf-8"))["status"] == "ok"


@pytest.mark.parametrize(
    ("problem", "phase"),
    [
        ("low resolution (table scale 0.5)", "layout"),
        ("date unread ('x')", "header"),
        ("soft: day and month could be swapped in 'x', assumed y", "header"),
        ("duration unread ('x')", "header"),
        ("winner not found", "teams"),
        ("team logo not recognised", "teams"),
        ("player name unread", "names"),
        ("bob: hero not on roster ('x')", "names"),
        ("bob: low confidence in name or hero (0.50)", "names"),
        ("expected 1 MVP, found 0", "badges"),
        ("expected 2 key players, found 1", "badges"),
        ("bob: low confidence in a stat (0.50)", "stats"),
        ("bob: unreadable souls ('x')", "stats"),
        ("soft: A kills 1 != B deaths 2", "stats"),
        ("soft: bob: name may be misread (0.81)", "names"),
        ("soft: A: souls total is cut off, so the sum is unchecked", "stats"),
        ("Hidden King: souls do not sum to 3k", "stats"),
    ],
)
def test_each_problem_lands_in_the_right_phase(problem: str, phase: str) -> None:
    assert phase_of(problem) == phase


def test_a_player_named_like_a_check_does_not_change_the_phase() -> None:
    """Phases come from message text, so a player's name must not be able to redirect one."""
    assert phase_of("MVP: unreadable souls ('x')") == "stats"


def test_flagged_mode_only_writes_reports_for_problem_images(tmp_path: Path) -> None:
    images = tmp_path / "in"
    images.mkdir()
    Image.open(SAMPLE).convert("RGB").save(images / "good.png")
    Image.fromarray(np.zeros((1080, 1920, 3), dtype=np.uint8)).save(images / "junk.png")
    out = tmp_path / "out"
    args = ["run", str(images), "--out", str(out), "--workers", "1", "--reports", "flagged"]
    assert main(args) == 0
    assert [p.name.split("_")[0] for p in (out / "images").iterdir()] == ["junk"]
