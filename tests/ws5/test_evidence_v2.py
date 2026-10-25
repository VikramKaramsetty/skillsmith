"""WS5.3: evidence v2 package (classification first, phases, key frames, expected vs actual, lessons).

The golden test runs the block-tuned stack v1 on the fake world twice: once as the passing check
run and once with rim_slip (the demo failure). WS4's classifier and WS3's key frames are not
built yet, so the test fills `phases`, `classification` and `keyframes` the way they will.
Regenerate the golden file with `UPDATE_GOLDEN=1 uv run pytest tests/ws5/test_evidence_v2.py`.
"""

import copy
import os
import re
from pathlib import Path

import fake_droid_sdk as fake
import pytest

from skillsmith.config import Config, load_config
from skillsmith.demo import demo_sources, temp_library
from skillsmith.events import NullBus
from skillsmith.hal.base import Env, make_env
from skillsmith.hal.fake import FakeWorld
from skillsmith.repair import droid_sdk as author_mod
from skillsmith.repair.approvers import AutoApprover
from skillsmith.repair.droid_sdk import DroidSDKAuthor
from skillsmith.repair.loop import RepairLoop
from skillsmith.repair.protocols import AuthorContext
from skillsmith.repair.report import attempt_images, attempt_report
from skillsmith.repair.scripted import ScriptedAuthor
from skillsmith.repair.workspace import prepare_workspace
from skillsmith.skills.library import SkillLibrary
from skillsmith.skills.runtime import run_skill
from skillsmith.tasks import parse_task
from skillsmith.types import Classification, PhaseCheck

GOLDEN = Path(__file__).parent / "golden" / "evidence_v2_rim_slip.md"
TASK = parse_task("stack the blue cup on the red cup")
V1, V2 = demo_sources()
LESSONS = ("# Lessons Droid has learned\n\nheader text\n\n"
           "- 2026-10-20 (stack the red block on the green block): Hover 10 cm above before descending.\n"
           "- 2026-10-23 (put the ball in the cup): Release only after the place descent finishes.\n")


def tmp_cfg(tmp_path) -> Config:
    data = copy.deepcopy(load_config().raw())
    data["repair"]["droid_ws_root"] = str(tmp_path / "droid_ws")
    data["repair"]["lessons_file"] = str(tmp_path / "lessons.md")
    data["repair"]["runs_dir"] = str(tmp_path / "runs")
    return Config(data)


def _run(cfg, rim_slip):
    w = FakeWorld.demo(cfg, rim_slip=rim_slip)
    return run_skill(V1, Env("fake", w, w, w), TASK.params, cfg, NullBus(), skill="stack", version=0,
                     success=TASK.success)


def _phases(ok_carry: bool):
    carry = ("blue cup held through 16 cm sideways move" if ok_carry
             else "lost blue cup during 16 cm sideways move (grip at 88% of cup height)")
    phases = [PhaseCheck("approach", True, "hovered 11 cm above blue cup", 1, 3),
              PhaseCheck("grasp", True, "closed at z=0.070, holding", 4, 4),
              PhaseCheck("lift", True, "lifted 11 cm, holding", 5, 5),
              PhaseCheck("carry", ok_carry, carry, 6, 6)]
    if ok_carry:
        phases += [PhaseCheck("place", True, "descended onto red cup", 7, 7),
                   PhaseCheck("release", True, "opened, blue cup stayed", 8, 8)]
    return phases


@pytest.fixture
def rim_slip():
    """(expected passing check run, failed rim-slip attempt) with WS3/WS4 outputs filled in."""
    cfg = load_config()
    expected, attempt = _run(cfg, rim_slip=False), _run(cfg, rim_slip=True)
    assert expected.ok and not attempt.ok
    expected.phases = _phases(ok_carry=True)
    expected.keyframes = {"carry": "/frames/sim/carry.jpg", "release": "/frames/sim/release.jpg"}
    attempt.phases = _phases(ok_carry=False)
    attempt.classification = Classification(
        "SLIP_IN_CARRY", "skill", "carry", 6,
        "grasp at z=0.07 (88% of cup height) held, lost during a 16 cm sideways move",
        hint="grip height", confidence=0.9)
    attempt.keyframes = {"grasp": "/frames/real/grasp.jpg", "carry": "/frames/real/carry.jpg",
                         "crop_before": "/frames/real/crop_before.jpg",
                         "crop_after": "/frames/real/crop_after.jpg"}
    return expected, attempt


def _normalise(md: str) -> str:
    return re.sub(r"'t': [0-9.]+", "'t': T", md)  # note() results carry a wall-clock time


def test_golden_rim_slip_report(rim_slip):
    expected, attempt = rim_slip
    md = _normalise(attempt_report(TASK, attempt, ["v0 on sim: ok"], expected=expected, lessons=LESSONS))
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(md, encoding="utf-8")
    assert md == GOLDEN.read_text(encoding="utf-8")


def test_sections_come_in_evidence_v2_order(rim_slip):
    expected, attempt = rim_slip
    md = attempt_report(TASK, attempt, ["v0 on sim: ok"], expected=expected, lessons=LESSONS)
    order = ["**Failure class:** `SLIP_IN_CARRY`", "## Phases", "**Task:**", "**Camera check:**",
             "## Key frames", "## Expected vs actual", "## Trace", "## Scene before", "## Scene after",
             "## Earlier rounds", "## Lessons from earlier tasks"]
    positions = [md.index(s) for s in order]
    assert positions == sorted(positions)
    assert md.splitlines()[2].startswith("**Failure class:**")  # right under the title


def test_failing_phase_frame_and_call_are_pointed_out(rim_slip):
    expected, attempt = rim_slip
    md = attempt_report(TASK, attempt, [], expected=expected)
    frames = md[md.index("## Key frames"):md.index("## Expected vs actual")]
    assert frames.index("/frames/real/carry.jpg") < frames.index("/frames/real/grasp.jpg")  # failing phase first
    assert "![before](/frames/real/crop_before.jpg)" in frames
    assert "| **carry** ← | ✗ | #6–#6 |" in md
    assert "**← failed here**" in md.split("| 6 | move_to |")[1].splitlines()[0]


def test_expected_vs_actual_has_numbers_and_frame_pairs(rim_slip):
    expected, attempt = rim_slip
    md = attempt_report(TASK, attempt, [], expected=expected)
    # on_top(blue cup, red cup): blue cup wanted on the red cup's top, ended 16 cm away
    assert "| blue cup | on top of red cup | (0.220, 0.080, 0.080) |" in md
    assert "| 16.0 |" in md
    assert "| carry | ✓ blue cup held through 16 cm sideways move | ✗ lost blue cup" in md
    assert "| release | ✓ opened, blue cup stayed | — (not reached) |" in md
    assert "![expected carry (fake)](/frames/sim/carry.jpg)" in md
    assert "![actual carry (fake)](/frames/real/carry.jpg)" in md
    assert "_no fake frame for release_" in md


def test_unclassified_failure_still_leads_with_a_class():
    attempt = _run(load_config(), rim_slip=True)
    attempt.phases, attempt.classification = [], None  # analysis gave nothing (WS4.5 labels this run)
    md = attempt_report(TASK, attempt, [])
    assert md.splitlines()[2].startswith("**Failure class:** `UNKNOWN` (unclassified")
    assert "_no phase checks for this attempt_" in md
    assert "## Key frames" not in md and "## Lessons" not in md
    assert "## Expected vs actual" in md  # positions come from the success check alone


def test_low_confidence_and_escalation_are_flagged(rim_slip):
    _, attempt = rim_slip
    attempt.classification = Classification("UNKNOWN", "unknown", None, None, "blue cup ended 16 cm off",
                                            confidence=0.3, escalate=True)
    md = attempt_report(TASK, attempt, [])
    assert "low confidence" in md and "Change the approach" in md


def test_lessons_show_only_the_latest(rim_slip):
    _, attempt = rim_slip
    many = "# Lessons\n\n" + "".join(f"- lesson {i}\n" for i in range(20))
    md = attempt_report(TASK, attempt, [], lessons=many)
    assert "latest 12 of 20" in md and "- lesson 19" in md and "- lesson 7\n" not in md


def _attempt_with_frames(cfg, frames: dict):
    attempt = _run(cfg, rim_slip=True)
    attempt.phases = _phases(ok_carry=False)
    attempt.classification = Classification("SLIP_IN_CARRY", "skill", "carry", 6, "lost blue cup in carry")
    attempt.keyframes = {k: str(v) for k, v in frames.items()}
    return attempt


def test_images_are_copied_into_the_workspace(tmp_path):
    cfg = tmp_cfg(tmp_path)
    run = tmp_path / "runs" / "run-1"
    run.mkdir(parents=True)
    frame = run / "carry.jpg"
    frame.write_bytes(b"\xff\xd8jpeg")
    attempt = _attempt_with_frames(cfg, {"carry": frame, "grasp": run / "gone.jpg"})
    assert attempt_images(attempt) == [str(frame), str(run / "gone.jpg")]  # failing phase first
    report = f"# r\n![carry: lost cup]({frame})\n![grasp]({run / 'gone.jpg'})\n"
    ctx = AuthorContext("run-1", 1, None, None, {}, attempt=attempt)
    ws = prepare_workspace(cfg, V1, report, report, ctx)
    text = (ws / "failure.md").read_text()
    assert "![carry: lost cup](frames/round1_carry.jpg)" in text
    assert "_(missing)_" in text
    assert (ws / "frames" / "round1_carry.jpg").read_bytes() == b"\xff\xd8jpeg"
    assert "frames/round1_carry.jpg" in (ws / "history" / "round_1.md").read_text()


def test_droid_gets_key_frames_as_images(tmp_path, monkeypatch):
    droid = fake.install(monkeypatch)
    monkeypatch.setattr(author_mod.shutil, "which", lambda name: f"/opt/bin/{name}")
    cfg = tmp_cfg(tmp_path)
    run = tmp_path / "runs" / "run-1"
    run.mkdir(parents=True)
    frames = {"carry": run / "carry.png", "grasp": run / "grasp.jpg"}
    for p in frames.values():
        p.write_bytes(b"img")
    attempt = _attempt_with_frames(cfg, frames)
    droid.turns = [fake.Turn(skill=V2, output=fake.output())]
    author = DroidSDKAuthor(cfg, NullBus(), lambda s: make_env("fake", cfg, s))
    try:
        ctx = AuthorContext("run-1", 1, FakeWorld.demo(cfg).observe(), None, {}, attempt=attempt)
        author.repair(TASK, V1, "no links in this report", ctx)
    finally:
        author.close()
    images = droid.sessions[0].prompts[0][1]["images"]
    assert [i.source.media_type for i in images] == ["image/png", "image/jpeg"]  # failing phase (carry) first


SECRET = b"host secret, not evidence"


def test_lesson_image_link_outside_runs_attaches_and_copies_nothing(tmp_path, monkeypatch):
    """WS5.11: a lesson linking a host file must not get that file into Droid's workspace or call."""
    droid = fake.install(monkeypatch)
    monkeypatch.setattr(author_mod.shutil, "which", lambda name: f"/opt/bin/{name}")
    cfg = tmp_cfg(tmp_path)
    secret = tmp_path / "outside" / "secret.png"
    secret.parent.mkdir()
    secret.write_bytes(SECRET)
    Path(cfg.repair.lessons_file).write_text(
        LESSONS + f"- 2026-10-24 (stack): Look here ![x]({secret}) first.\n")
    attempt = _attempt_with_frames(cfg, {})
    report = attempt_report(TASK, attempt, [], lessons=Path(cfg.repair.lessons_file).read_text())
    report += f"\n![smuggled]({secret})\n"  # even a raw link in the report text is not followed
    droid.turns = [fake.Turn(skill=V2, output=fake.output(lesson=f"Use ![y]({secret}) <img src=x> always."))]
    author = DroidSDKAuthor(cfg, NullBus(), lambda s: make_env("fake", cfg, s))
    try:
        ctx = AuthorContext("run-1", 1, FakeWorld.demo(cfg).observe(), str(secret), {}, attempt=attempt)
        author.repair(TASK, V1, report, ctx)
    finally:
        author.close()
    assert "images" not in droid.sessions[0].prompts[0][1]  # nothing attached, snapshot included
    ws = author.workspace("run-1")
    assert not (ws / "frames").exists() and not (ws / "snapshot.jpg").exists()
    assert all(SECRET not in f.read_bytes() for f in ws.rglob("*") if f.is_file())
    assert str(secret) not in (ws / "failure.md").read_text()
    saved = Path(cfg.repair.lessons_file).read_text().splitlines()[-1]
    assert saved.endswith("Use y always.") and "](" not in saved and "<img" not in saved


def test_frames_outside_runs_dir_are_refused(tmp_path):
    cfg = tmp_cfg(tmp_path)
    outside = tmp_path / "elsewhere.jpg"
    outside.write_bytes(SECRET)
    (tmp_path / "runs").mkdir()
    escape = tmp_path / "runs" / ".." / "elsewhere.jpg"
    attempt = _attempt_with_frames(cfg, {"carry": outside, "grasp": escape})
    report = attempt_report(TASK, attempt, [])
    ws = prepare_workspace(cfg, V1, report, report, AuthorContext("run-1", 1, None, None, {}, attempt=attempt))
    assert not (ws / "frames").exists()
    assert "image link removed" in (ws / "failure.md").read_text()


def test_loop_feeds_sim_check_and_lessons_into_the_evidence(tmp_path):
    cfg = tmp_cfg(tmp_path)
    Path(cfg.repair.lessons_file).write_text(LESSONS)
    author = ScriptedAuthor([V1], [V2])
    w = FakeWorld.demo(cfg, rim_slip=True)
    loop = RepairLoop(cfg, Env("fake", w, w, w), lambda s: make_env("fake", cfg, s),
                      SkillLibrary(temp_library()), author, AutoApprover("fake"), NullBus())
    assert loop.run(TASK).ok
    report = author.reports[0]
    assert "### This skill passed on fake (v0) but failed on fake" in report
    assert "Release only after the place descent finishes." in report
