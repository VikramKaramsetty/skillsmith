"""WS5.4: Droid's evidence tools: get_frame(phase), get_object_track(name), compare_with_success(phase).

Runs the block-tuned stack v1 on the fake world (rim_slip = the demo failure) with WS3/WS4 outputs
filled in the way test_evidence_v2 does, plus real frame files so copying into the workspace is tested.
"""

import asyncio
import re
from pathlib import Path

import fake_droid_sdk as fake
import pytest
from test_evidence_v2 import TASK, V1, V2, _phases, _run, tmp_cfg

from skillsmith.config import load_config
from skillsmith.demo import temp_library
from skillsmith.events import NullBus
from skillsmith.hal.base import Env, make_env
from skillsmith.hal.fake import FakeWorld
from skillsmith.repair import droid_sdk as author_mod
from skillsmith.repair import evidence_tools as et
from skillsmith.repair.approvers import AutoApprover
from skillsmith.repair.droid_sdk import DroidSDKAuthor
from skillsmith.repair.droid_tools import TOOL_NAMES, RepairTools, decide
from skillsmith.repair.loop import RepairLoop
from skillsmith.repair.protocols import RepairContext
from skillsmith.repair.scripted import ScriptedAuthor
from skillsmith.skills.library import SkillLibrary
from skillsmith.types import Classification


@pytest.fixture
def runs(tmp_path):
    """(passing check run, failed rim-slip attempt), with phases, classification and frame files."""
    cfg = load_config()
    expected, attempt = _run(cfg, rim_slip=False), _run(cfg, rim_slip=True)
    frames = tmp_path / "runs" / "frames_src"  # under runs_dir (tmp_cfg), as WS5.11 requires
    frames.mkdir(parents=True)
    for name in ("sim_carry.jpg", "real_carry.jpg", "real_grasp.jpg"):
        (frames / name).write_bytes(name.encode())
    expected.phases = _phases(ok_carry=True)
    expected.keyframes = {"carry": str(frames / "sim_carry.jpg")}
    attempt.phases = _phases(ok_carry=False)
    attempt.classification = Classification("SLIP_IN_CARRY", "skill", "carry", 6,
                                             "lost blue cup during a 16 cm sideways move", confidence=0.9)
    attempt.keyframes = {"carry": str(frames / "real_carry.jpg"), "grasp": str(frames / "real_grasp.jpg"),
                         "crop_after": str(tmp_path / "runs" / "never_saved.jpg")}
    return expected, attempt


# ---- get_frame ---------------------------------------------------------------------------------
def test_get_frame_never_copies_a_frame_outside_runs_dir(runs, tmp_path):
    _, attempt = runs
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"secret")
    attempt.keyframes["carry"] = str(outside)
    ws = tmp_path / "ws"
    out = et.get_frame(attempt, "carry", ws, 1, roots=[(tmp_path / "runs").resolve()])
    assert "not copied: outside runs_dir" in out and str(outside) not in out
    assert not (ws / "frames").exists()


def test_get_frame_copies_the_frame_and_shows_the_phase(runs, tmp_path):
    _, attempt = runs
    ws = tmp_path / "ws"
    out = et.get_frame(attempt, "Carry ", ws, 1)
    assert "**Check:** ✗ lost blue cup during 16 cm sideways move" in out
    assert "`frames/round1_fake_carry_real_carry.jpg` (image; described here as text)" in out
    assert "Read" not in out
    assert (ws / "frames" / "round1_fake_carry_real_carry.jpg").read_bytes() == b"real_carry.jpg"
    calls = out.split("## Calls in this phase")[1]
    assert "| 6 | move_to |" in calls and "**← failed here**" in calls and "| 5 |" not in calls


def test_get_frame_defaults_to_the_failing_phase_and_lists_what_exists(runs, tmp_path):
    _, attempt = runs
    assert et.get_frame(attempt, "", tmp_path, 1).startswith("# carry on fake")
    assert "_no frame captured for this phase_" in et.get_frame(attempt, "lift", tmp_path, 1)
    assert "_(missing" in et.get_frame(attempt, "crop_after", tmp_path, 1)
    missing = et.get_frame(attempt, "wiggle", tmp_path, 1)
    assert "No phase or frame named 'wiggle'" in missing and "approach, grasp, lift, carry, crop_after" in missing
    assert et.get_frame(None, "carry", tmp_path, 1) == et.NO_ATTEMPT


def test_get_frame_describes_what_the_frame_shows(runs, tmp_path):
    _, attempt = runs
    out = et.get_frame(attempt, "carry", tmp_path, 1).split("## Calls in this phase")[0]
    assert "Objects, from the scene after the attempt (this phase ends the run):" in out
    assert "| blue cup | 0.220 | -0.080 | 0.000 | 0.080 | 0.065 | 16.0 |" in out
    assert "- Gripper tip (after call #6 move_to): (0.220, 0.080, 0.180)" in out
    assert "- Planned target (call #6 move_to): (0.220, 0.080, 0.180)" in out
    assert "- Error arrow, tip → target: dx +0.0, dy +0.0, dz +0.0 cm (0.0 cm in xy" in out
    assert "- Nearest object to the tip: red cup, 0.0 cm away in xy; tip +10.0 cm from its top" in out
    grasp = et.get_frame(attempt, "grasp", tmp_path, 1)
    assert "Objects, from the scene before the attempt (no look() before this phase ended):" in grasp
    assert "- Nearest object to the tip: blue cup, 0.0 cm away in xy; tip -1.0 cm from its top" in grasp
    assert "Planned target" not in grasp  # no move_to in the grasp phase
    assert "Objects, from the scene after the attempt:" in et.get_frame(attempt, "crop_after", tmp_path, 1)


def test_error_arrow_points_from_the_tip_to_the_target(runs, tmp_path):
    _, attempt = runs
    move = attempt.trace[6]
    move.ee_after = (0.205, 0.090, 0.175)  # stopped 1.5 cm short in x, 1 cm over in y, 0.5 cm low
    out = et.get_frame(attempt, "carry", tmp_path, 1)
    assert "- Error arrow, tip → target: dx +1.5, dy -1.0, dz +0.5 cm (1.8 cm in xy, 1.9 cm in 3D)" in out


# ---- get_object_track --------------------------------------------------------------------------
def test_object_track_shows_where_the_cup_left_the_gripper(runs):
    _, attempt = runs
    out = et.get_object_track(attempt, "blue cup")
    assert "| start | scene before | (0.220, -0.080, 0.000) |" in out
    assert "| #4 | grasped (matched by tip position) |" in out
    assert "| #6 | held: move_to | with gripper (inferred) | (0.220, 0.080, 0.180) | 16.0 |" in out
    assert "| end | scene after |" in out
    assert "ended 16.0 cm (xy) from the tip's position after call #6" in out


def test_object_track_of_an_object_never_grasped_and_unknown_names(runs):
    _, attempt = runs
    red = et.get_object_track(attempt, "red cup")
    assert "grasped" not in red and "Start → end: moved 0.0 cm" in red
    assert et.get_object_track(attempt, "green ball").startswith("No object matching 'green ball'. Seen: blue cup")
    assert et.get_object_track(attempt, "").startswith("No object matching ''")


def test_object_track_on_success_ends_released(runs):
    expected, _ = runs
    out = et.get_object_track(expected, "blue cup")
    assert "| released |" in out and "Never released" not in out


# ---- compare_with_success ----------------------------------------------------------------------
def test_compare_puts_the_phase_side_by_side(runs, tmp_path):
    expected, attempt = runs
    out = et.compare_with_success(attempt, expected, "sim gate", "carry", tmp_path, 2)
    assert out.startswith("# carry: passed on fake (v0, sim gate) vs failed on fake (v0)")
    assert "| check | ✓ blue cup held through 16 cm sideways move (calls #6–#6) | ✗ lost blue cup" in out
    assert "| 1 | #6 move_to(0.220, 0.080, 0.180) |" in out
    assert "expected (fake): `frames/round2_expected_fake_carry_sim_carry.jpg`" in out
    assert "; shows tip (0.220, 0.080, 0.180); nearest object: red cup, 0.0 cm away" in out
    assert "actual (fake): `frames/round2_actual_fake_carry_real_carry.jpg`" in out
    assert "| blue cup | (0.220, 0.080, 0.080) | (0.220, -0.080, 0.000) | 17.9 |" in out


def test_compare_marks_phases_the_failed_run_never_reached(runs, tmp_path):
    expected, attempt = runs
    out = et.compare_with_success(attempt, expected, "sim gate", "release", tmp_path, 1)
    assert "| check | ✓ opened, blue cup stayed (calls #8–#8) | — (not reached) |" in out
    assert "| 1 | #8 release() | (0.220, 0.080, 0.155) | — | — | — |" in out
    assert "No phase named 'wiggle'" in et.compare_with_success(attempt, expected, "x", "wiggle", tmp_path, 1)


def test_compare_without_phases_uses_the_whole_trace(tmp_path):
    cfg = load_config()
    expected, attempt = _run(cfg, rim_slip=False), _run(cfg, rim_slip=True)
    out = et.compare_with_success(attempt, expected, "sim gate", "", tmp_path, 1)
    assert "# whole run:" in out and "comparing the whole trace" in out
    assert "| 8 | #7 move_to(0.220, 0.080, 0.155) |" in out and out.count("| — | — | — |") == 4
    assert et.compare_with_success(attempt, None, "", "", tmp_path, 1) == et.NO_REFERENCE


# ---- RepairTools, policy and wiring ------------------------------------------------------------
def test_repair_tools_serve_the_round_and_fall_back_to_droids_own_passing_sim(runs, tmp_path):
    cfg = load_config()
    expected, attempt = runs
    (tmp_path / "skill.py").write_text(V2)
    library = {"pick_place": Path(cfg.path("skills/library/pick_place.py")).read_text()}
    t = RepairTools(cfg, NullBus(), tmp_path, TASK, library, lambda s: make_env("fake", cfg, s), sim_budget=1)
    assert t.get_frame("carry") == et.NO_ATTEMPT
    t.update(FakeWorld.demo(cfg).observe(), 1, library, attempt=attempt)
    assert t.get_frame().startswith("# carry on fake") and "Track of blue cup" in t.get_object_track("blue cup")
    assert t.compare_with_success("carry") == et.NO_REFERENCE
    assert t.simulate_skill().startswith("PASS")
    assert "your last passing simulate_skill run" in t.compare_with_success("carry")
    t.update(None, 2, library, attempt=attempt, expected=expected)
    assert "the sim gate run of this skill" in t.compare_with_success("carry")
    assert [name for name, _, _ in t.functions()] == list(TOOL_NAMES)


class McpToolAction:
    def __init__(self, tool_name, server_name=None):
        self.tool_name, self.server_name, self.actual_tool_name = tool_name, server_name, tool_name


def test_policy_allows_the_evidence_tools(tmp_path):
    for name in ("get_frame", "get_object_track", "compare_with_success"):
        assert decide(McpToolAction(name, "skillsmith"), tmp_path)[0]
        assert decide(McpToolAction(f"skillsmith___{name}"), tmp_path)[0]


class Capture(ScriptedAuthor):
    def __init__(self, *a):
        super().__init__(*a)
        self.contexts = []

    def repair(self, task, source, report_md, ctx):
        self.contexts.append(ctx)
        return super().repair(task, source, report_md, ctx)


def test_loop_hands_the_failed_attempt_and_the_passing_sim_run_to_the_author(tmp_path):
    cfg = tmp_cfg(tmp_path)
    author = Capture([V1], [V2])
    w = FakeWorld.demo(cfg, rim_slip=True)
    loop = RepairLoop(cfg, Env("fake", w, w, w), lambda s: make_env("fake", cfg, s),
                      SkillLibrary(temp_library()), author, AutoApprover("fake"), NullBus())
    result = loop.run(TASK)
    assert result.ok
    ctx = author.contexts[0]
    assert isinstance(ctx, RepairContext)
    assert ctx.attempt is result.attempts[0] and not ctx.attempt.ok
    assert ctx.expected is not None and ctx.expected.ok and ctx.expected.version == 0


def test_droid_calls_the_evidence_tools_through_the_sdk(runs, tmp_path, monkeypatch):
    droid = fake.install(monkeypatch)
    monkeypatch.setattr(author_mod.shutil, "which", lambda name: f"/opt/bin/{name}")
    cfg = tmp_cfg(tmp_path)
    expected, attempt = runs
    droid.turns = [fake.Turn(skill=V2, output=fake.output(),
                             tools=["get_frame", "get_object_track", "compare_with_success"])]
    bus = NullBus()
    author = DroidSDKAuthor(cfg, bus, lambda s: make_env("fake", cfg, s))
    try:
        ctx = RepairContext("run-1", 1, FakeWorld.demo(cfg).observe(), None, {}, attempt=attempt, expected=expected)
        author.repair(TASK, V1, "# failure", ctx)
    finally:
        author.close()
    results = [e["data"]["content"] for e in bus.events if e["type"] == "droid.tool_result"]
    assert "# carry on fake (v0)" in results[0]
    assert "No object matching ''" in results[1]
    assert "# carry: passed on fake (v0, the sim gate run of this skill)" in results[2]
    assert (author.workspace("run-1") / "frames" / "round1_fake_carry_real_carry.jpg").is_file()


def test_get_frame_output_reaches_the_model_through_the_sdk(runs, tmp_path, monkeypatch):
    """Done-when for #156: Droid's get_frame call returns the frame as text the model receives."""
    droid = fake.install(monkeypatch)
    monkeypatch.setattr(author_mod.shutil, "which", lambda name: f"/opt/bin/{name}")
    cfg = tmp_cfg(tmp_path)
    _, attempt = runs
    droid.turns = [fake.Turn(skill=V2, output=fake.output(), tools=["get_frame", "read_file"],
                             tool_args={"get_frame": {"phase": "carry"},
                                        "read_file": {"name": "frames/round1_fake_carry_real_carry.jpg"}})]
    author = DroidSDKAuthor(cfg, NullBus(), lambda s: make_env("fake", cfg, s))
    try:
        ctx = RepairContext("run-1", 1, FakeWorld.demo(cfg).observe(), None, {}, attempt=attempt)
        author.repair(TASK, V1, "# failure", ctx)
    finally:
        author.close()
    (frame_name, frame), (read_name, read) = droid.sessions[0].tool_results
    assert frame_name == "get_frame" and [block["type"] for block in frame] == ["text"]
    text = frame[0]["text"]
    assert text.startswith("# carry on fake (v0)") and "## What the frame shows (as text)" in text
    assert "| blue cup | 0.220 | -0.080 | 0.000 | 0.080 | 0.065 | 16.0 |" in text
    assert "- Error arrow, tip → target:" in text and "Read" not in text
    # The image itself stays unreadable to Droid, so nothing may point it at the file.
    assert read_name == "read_file" and "is an image" in read[0]["text"]


def test_droid_template_names_only_the_sdk_tools_and_the_current_rules():
    from skillsmith.repair.workspace import TEMPLATE_DIR

    text = "\n".join((TEMPLATE_DIR / name).read_text() for name in ("AGENTS.md", "API.md"))
    assert not re.search(r"\b(Read|Edit)\b", text)
    for name in TOOL_NAMES:
        assert f"`{name}(" in text
    for rule in ("No decorators", "No recursion", "`.format`", "No attribute assignment", "|e| ≤ 10",
                 "Top-level assignments are constants only", "same name"):
        assert rule in text


def test_real_sdk_tool_results_are_text_only(runs, tmp_path):
    """Why get_frame describes the frame: droid-sdk 0.5 tools cannot return image content."""
    mcp = pytest.importorskip("droid_sdk.mcp")
    assert mcp.ToolResponse.__dataclass_fields__["content"].type in (str, "str")
    cfg = load_config()
    _, attempt = runs
    t = RepairTools(cfg, NullBus(), tmp_path, TASK, {}, lambda s: make_env("fake", cfg, s))
    t.update(None, 1, {}, attempt=attempt)
    get_frame = next(mcp.tool(n, d, fn) for n, d, fn in t.functions() if n == "get_frame")
    out = asyncio.run(get_frame.handler({"phase": "carry"}))
    assert isinstance(out, str) and "## What the frame shows (as text)" in out
