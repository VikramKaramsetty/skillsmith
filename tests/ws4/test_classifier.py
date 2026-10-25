"""WS4.5: failure classifier, one fake-world test per class the fake can produce, asserting class,
bucket, phase and call index; the perception gate never lets a doubtful scene become a skill label."""

import math
from dataclasses import replace

import pytest

from skillsmith.analysis import analyse, analyse_attempt
from skillsmith.analysis.classify import MIN_SKILL_CONFIDENCE
from skillsmith.config import REPO_ROOT, Config, load_config
from skillsmith.demo import demo_sources
from skillsmith.events import EventBus, NullBus
from skillsmith.hal.base import Env
from skillsmith.hal.fake import FakeWorld, _Obj
from skillsmith.repair.benchmark import CASES
from skillsmith.skills.runtime import run_skill
from skillsmith.types import (
    BUCKET_ENVIRONMENT,
    BUCKET_SKILL,
    BUCKET_UNKNOWN,
    FAILURE_CLASSES,
    AttemptResult,
    SafetyIssue,
    SafetyReport,
)

BASE = load_config()
cfg = Config({**BASE.raw(), "analysis": {"classifier": True}})  # off by default until the lead enables it
PICK_PLACE = (REPO_ROOT / "skills/library/pick_place.py").read_text()
CUPS = {"top": "blue cup", "bottom": "red cup"}
ON_TOP = {"op": "on_top", "args": ["blue cup", "red cup"]}
IN_CUP = {"op": "inside", "args": ["green block", "blue cup"]}
SLOT_WORDS = ["grip height", "grip z", "travel height", "travel z", "place x", "place y", "place z",
              "place height", "grasp call"]  # #157: a hint points at evidence, never at a code knob


def _run(source, params=None, *, world=None, rim_slip=False, success=None, skill="t", bus=None):
    w = world or FakeWorld.demo(cfg, rim_slip=rim_slip)
    return run_skill(source, Env("fake", w, w, w), params or {}, cfg, bus or NullBus(), skill=skill,
                     library={"pick_place": PICK_PLACE}, success=success)


def _skill(*lines):
    body = "\n    ".join(lines)
    return f"SKILL = {{}}\n\ndef run(robot, scene, skills):\n    {body}\n"


def _expect(attempt, cls, phase, call_index):
    c = attempt.classification
    assert c is not None, attempt.reason()
    assert (c.cls, c.bucket, c.phase, c.call_index) == (cls, FAILURE_CLASSES[cls], phase, call_index), c
    assert c.summary and c.hint
    assert not [w for w in SLOT_WORDS if w in c.hint.lower()], c.hint
    return c


def _demo(world_cls, **kw):
    """The demo table in a FakeWorld subclass."""
    return world_cls(FakeWorld.demo(cfg).objects, cfg, **kw)


def _call(attempt, name, nth=0):
    return [c for c in attempt.trace if c.name == name][nth].index


# ---- skill bucket ---------------------------------------------------------------------------

def test_classifier_is_off_until_the_config_enables_it():
    off = Config({k: v for k, v in BASE.raw().items() if k != "analysis"})  # no `analysis.classifier` key
    w = FakeWorld.demo(off, rim_slip=True)
    r = run_skill(demo_sources()[0], Env("fake", w, w, w), CUPS, off, NullBus(), skill="stack", success=ON_TOP)
    assert not r.ok and r.classification is None and r.phases == []
    with pytest.raises(NotImplementedError, match="analysis.classifier"):
        analyse_attempt(r, off)
    assert analyse(r, off)[1].cls == "SLIP_IN_CARRY"  # analyse() always runs, flag or not


def test_success_has_no_classification():
    r = _run(demo_sources()[1], CUPS, rim_slip=True, success=ON_TOP, skill="stack")
    assert r.ok and r.classification is None and r.phases


def test_rim_slip_is_slip_in_carry_at_the_carry_move_with_numbers():
    r = _run(demo_sources()[0], CUPS, rim_slip=True, success=ON_TOP, skill="stack")
    carry = next(c for c in r.trace if c.name == "move_to" and c.args["y"] == 0.08)  # the sideways move
    c = _expect(r, "SLIP_IN_CARRY", "carry", carry.index)
    assert c.summary == ("blue cup gripped at z=0.070 (88% of its height) was held, then lost during a 16.0 cm "
                         "sideways move; it ended 16.0 cm from the gripper")
    assert "88%" in c.hint and c.evidence["grip_fraction"] == 0.875 and c.confidence >= 0.9


def test_grasp_above_the_top_is_grasp_miss_at_the_grasp():
    miss = PICK_PLACE.replace("target.z + grip_offset", "target.top + 0.02")
    r = _run(miss, {"obj": "blue cup", "x": 0.22, "y": 0.08}, skill="pick_place")
    c = _expect(r, "GRASP_MISS", "grasp", _call(r, "grasp"))
    assert "2.0 cm above its top" in c.summary and "above the object's top" in c.hint


def test_grasp_beside_the_object_is_grasp_miss_with_the_xy_offset():
    miss = PICK_PLACE.replace("target.x, target.y, target.z + grip_offset", "target.x + 0.06, target.y, 0.04")
    r = _run(miss, {"obj": "blue cup", "x": 0.15, "y": 0.12}, skill="pick_place")
    c = _expect(r, "GRASP_MISS", "grasp", _call(r, "grasp"))
    assert "6.0 cm off blue cup centre (radius 3.2 cm)" in c.summary and "beside the object" in c.hint


def test_never_closing_the_gripper_is_no_pick():
    no_grasp = PICK_PLACE.replace("if not robot.grasp():", "if False:")
    r = _run(no_grasp, {"obj": "blue cup", "x": 0.22, "y": 0.08}, skill="pick_place")
    down = next(c for c in r.trace if c.name == "move_to" and c.args["z"] == 0.04)
    c = _expect(r, "NO_PICK", "grasp", down.index + 1)  # the move that left without closing
    assert f"(call #{down.index})" in c.summary and "never called" in c.summary


def test_carrying_too_low_is_a_collision_in_the_carry():
    low = PICK_PLACE.replace("TRAVEL_Z = 0.18", "TRAVEL_Z = 0.09")
    r = _run(low, {"obj": "blue cup", "x": 0.22, "y": 0.08}, skill="pick_place")
    failing = next(c for c in r.trace if not c.ok)
    assert failing.error.startswith("SafetyViolation: [CARRY_PATH]")  # WS4.3 guard stops it before contact
    c = _expect(r, "COLLISION", "carry", failing.index)  # a carry collision, not UNSAFE_TARGET
    assert c.summary.startswith("the carry-path guard stopped the move: carrying blue cup")
    assert "red cup" in c.summary and "clearance" in c.summary and "[CARRY_PATH]" not in c.summary
    assert "how high the held object travels" in c.hint and c.evidence["stopped_by"] == "carry-path guard"


def test_a_carry_path_audit_issue_is_a_collision_in_the_carry():
    message = "carrying blue cup, move (0.2, 0.1, 0.09) -> (0.2, -0.1, 0.09) (call 3) sweeps through red cup"
    a = replace(_synthetic(error=None), safety=SafetyReport(ok=False, issues=[SafetyIssue("CARRY_PATH", message, 3)]))
    c = analyse_attempt(a, cfg)[1]
    assert (c.cls, c.bucket, c.phase) == ("COLLISION", BUCKET_SKILL, "carry") and "sweeps through" in c.summary


def test_placing_into_the_support_is_a_collision_in_the_place():
    bad = PICK_PLACE.replace("place_z + grip_offset + 0.005", "place_z + 0.01")
    r = _run(bad, {"obj": "blue cup", "x": 0.22, "y": 0.08}, skill="pick_place")
    failing = next(c for c in r.trace if not c.ok)
    _expect(r, "COLLISION", "place", failing.index)


def test_placing_beside_the_target_is_place_off_target():
    r = _run(PICK_PLACE, {"obj": "blue cup", "x": 0.15, "y": 0.12, "place_z": 0.0}, success=ON_TOP,
             skill="pick_place")
    release = _call(r, "release", 1)
    c = _expect(r, "PLACE_OFF_TARGET", "place", release - 1)
    assert "blue cup is" in c.summary and "cm from red cup in xy" in c.summary


def test_setting_down_on_top_instead_of_inside_is_place_wrong_height():
    r = _run(PICK_PLACE, {"obj": "green block", "x": 0.22, "y": -0.08, "place_z": 0.08}, success=IN_CUP,
             skill="pick_place")
    release = _call(r, "release", 1)
    c = _expect(r, "PLACE_WRONG_HEIGHT", "place", release - 1)
    assert "resting on top of blue cup" in c.summary and c.evidence["fall"] <= 0.02


def test_letting_go_high_above_the_support_is_dropped_from_height():
    r = _run(PICK_PLACE, {"obj": "green block", "x": 0.22, "y": -0.08, "place_z": 0.15}, success=IN_CUP,
             skill="pick_place")
    c = _expect(r, "DROPPED_FROM_HEIGHT", "release", _call(r, "release", 1))
    assert c.summary.startswith("let go of green block 7.5 cm above where it landed")


def test_target_outside_the_workspace_is_unsafe_target():
    r = _run(_skill("robot.move_to(0.22, 0.0, 0.30)"))
    c = _expect(r, "UNSAFE_TARGET", "approach", 0)
    assert "[OUT_OF_BOX] z=0.300" in c.summary


def test_a_python_error_before_any_call_is_skill_error_with_the_line():
    r = _run(_skill('name = "blue cup"', "a = scene.require(name.label)"))
    c = _expect(r, "SKILL_ERROR", "approach", None)
    assert c.summary.startswith("AttributeError") and "t.py line 5" in c.summary


def test_a_runaway_loop_is_skill_error_from_the_call_budget():
    r = _run(_skill("for _ in range(50):", "    for _ in range(2):", "        robot.note('again')"))
    c = _expect(r, "SKILL_ERROR", "approach", cfg.safety.max_calls - 1)  # the last call that ran
    assert "CALL_BUDGET" in c.summary


def test_static_rejection_is_static_rejected():
    r = _run("import os\n" + _skill("pass"))
    c = _expect(r, "STATIC_REJECTED", "approach", None)
    assert "import" in c.summary.lower()


def test_timeout_is_timeout():
    a = _synthetic(error="SkillTimeout: attempt ran longer than 30.0 s (t.py line 5: while True:)")
    c = analyse_attempt(a, cfg)[1]
    assert (c.cls, c.bucket) == ("TIMEOUT", BUCKET_SKILL) and "30.0 s" in c.summary


def test_the_runtime_attempt_timeout_is_timeout():
    slow = Config({**cfg.raw(), "safety": {**cfg.raw()["safety"], "attempt_timeout_s": 0.05}})
    w = FakeWorld.demo(slow)
    spin = _skill("robot.home()", "for a in range(50):", "    for b in range(50):", "        for c in range(50):",
                  "            for d in range(50):", "                for e in range(50):", "                    pass")
    r = run_skill(spin, Env("fake", w, w, w), {}, slow, NullBus(), skill="t")  # WS4.2: AttemptTimeout
    assert r.error.startswith("AttemptTimeout:")
    c = r.classification
    assert (c.cls, c.bucket) == ("TIMEOUT", BUCKET_SKILL) and "wall-clock limit" in c.summary
    assert "loops" in c.hint


class BumpingWorld(FakeWorld):
    """A fake world with the live world model's bump stop: an open gripper sweeping through an object
    pushes it 3 cm; `shove` moves an object at a given move no matter where the arm is (a human)."""

    def __init__(self, *a, shove=None, **kw):
        super().__init__(*a, **kw)
        self.shove, self.moves, self.bumps = shove, 0, []

    def move_joints(self, target, speed_dps):
        super().move_joints(target, speed_dps)
        self.moves += 1
        x, y, z = target[0], target[1], target[2]
        for o in self._others():
            if o.contains_xy(x, y) and o.z <= z < o.top:
                o.x += 0.03
                self.bumps.append(f"{o.label} moved 3.0 cm toward +x while not held")
        if self.shove and self.moves == self.shove[1]:
            o = next(o for o in self.objects if o.label == self.shove[0])
            o.y += 0.04
            self.bumps.append(f"{o.label} moved 4.0 cm toward +y while not held")

    def unexpected_motion(self):
        message, self.bumps = "; ".join(self.bumps), []
        return message or None


def test_sweeping_through_an_object_before_the_grasp_is_knocked_on_approach():
    w = _demo(BumpingWorld)
    r = _run(_skill("robot.release()", "robot.move_to(0.15, 0.0, 0.02)", "robot.move_to(0.22, -0.08, 0.18)"),
             world=w)
    assert r.error.startswith("WorldChanged")
    c = _expect(r, "KNOCKED_ON_APPROACH", "approach", 1)
    assert c.summary.startswith("green block moved 3.0 cm when the tip passed 0.0 cm from it")


def test_an_object_moved_far_from_the_arm_is_bumped_by_something_else():
    w = _demo(BumpingWorld, shove=("red cup", 1))
    r = _run(_skill("robot.release()", "robot.move_to(0.20, -0.15, 0.18)", "robot.home()"), world=w)
    c = _expect(r, "BUMPED_BY_SOMETHING_ELSE", "approach", 2)
    assert "red cup moved 4.0 cm while the arm stayed at least" in c.summary


# ---- environment bucket ---------------------------------------------------------------------

def test_missing_object_is_object_not_found():
    r = _run(_skill('scene.require("purple cup")'))
    c = _expect(r, "OBJECT_NOT_FOUND", "approach", None)
    assert c.summary == "no 'purple cup' on the table (saw: blue cup, red cup, green block)"


def test_object_beyond_reach_is_object_out_of_reach():
    far = _Obj("far_cup_0", "far cup", "yellow", "cup", 0.36, 0.0, 0.0, 0.08, 0.065)
    w = FakeWorld([far], cfg)
    r = _run(PICK_PLACE, {"obj": "far cup", "x": 0.2, "y": 0.0}, world=w, skill="pick_place")
    c = _expect(r, "OBJECT_OUT_OF_REACH", "approach", _call(r, "move_to"))
    assert "far cup at (0.360, 0.000) is 36.0 cm from the base" in c.summary


class FlakyArm(FakeWorld):
    """robotd dropping the connection on the third move."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.moves = 0

    def move_joints(self, target, speed_dps):
        self.moves += 1
        if self.moves == 3:
            raise ConnectionResetError("robotd closed the connection")
        super().move_joints(target, speed_dps)


def test_a_dropped_robotd_connection_is_hardware_fault():
    r = _run(PICK_PLACE, {"obj": "blue cup", "x": 0.22, "y": 0.08}, world=_demo(FlakyArm), skill="pick_place")
    failing = next(c for c in r.trace if not c.ok)
    _expect(r, "HARDWARE_FAULT", "approach", failing.index)


class GatedWorld(FakeWorld):
    """A fake world with the WorldModel's confidence gate and detection confidences."""

    def __init__(self, *a, gate=(True, "ok"), confidence=1.0, vanish=None, **kw):
        super().__init__(*a, **kw)
        self.gate, self.confidence, self.vanish, self.observed = gate, confidence, vanish, 0

    def observe(self):
        scene = super().observe()
        self.observed += 1
        objects = tuple(replace(o, confidence=self.confidence) for o in scene.objects
                        if not (self.vanish and self.observed > 1 and o.label == self.vanish))
        return replace(scene, objects=objects)

    def perception_ok(self):
        if isinstance(self.gate, Exception):
            raise self.gate
        return self.gate


def _slip_on(world):
    """The demo's rim slip (a clear SLIP_IN_CARRY on a trusted scene) on a gated world."""
    return _run(demo_sources()[0], CUPS, world=world, success=ON_TOP, skill="stack")


@pytest.mark.parametrize("gate, cls", [
    ((False, "marker 2 is 1.8 cm from where it was calibrated"), "CAMERA_MOVED"),
    ((False, "camera moved: markers shifted 12 px"), "CAMERA_MOVED"),
    ((False, "calibration residual 2.4 cm"), "CALIBRATION_DRIFT"),
    ((False, "detections flickering"), "PERCEPTION_UNCERTAIN"),
    # WS3.6 WorldModel.perception_ok() wording
    ((False, "marker drift 1.8 cm (limit 1.0 cm): camera moved, recalibrate"), "CAMERA_MOVED"),
    ((False, "world model stale: no frames"), "PERCEPTION_UNCERTAIN"),
    ((False, "low confidence: blue cup at 0.31 (< 0.50) for 5 frames"), "PERCEPTION_UNCERTAIN"),
])
def test_a_failed_perception_gate_is_never_a_skill_label(gate, cls):
    r = _slip_on(_demo(GatedWorld, rim_slip=True, gate=gate))
    c = _expect(r, cls, "carry", r.trace[-1].index)
    assert c.bucket == BUCKET_ENVIRONMENT and gate[1] in c.summary


def test_low_detection_confidence_is_perception_uncertain_not_slip():
    r = _slip_on(_demo(GatedWorld, rim_slip=True, confidence=0.3))
    c = _expect(r, "PERCEPTION_UNCERTAIN", "carry", r.trace[-1].index)
    assert "confidence 0.30 (below 0.50)" in c.summary


def test_an_object_vanishing_is_perception_uncertain():
    r = _slip_on(_demo(GatedWorld, rim_slip=True, vanish="green block"))
    c = _expect(r, "PERCEPTION_UNCERTAIN", "carry", r.trace[-1].index)
    assert "3 before and 2 after (vanished: green block)" in c.summary


def test_a_gate_that_crashes_counts_as_failed_and_emits_an_error(tmp_path):
    events = []
    bus = EventBus("gate-crash", tmp_path)
    bus.subscribe(events.append)
    world = _demo(GatedWorld, rim_slip=True, gate=RuntimeError("no frames"))
    r = _run(demo_sources()[0], CUPS, world=world, success=ON_TOP, skill="stack", bus=bus)
    bus.close()
    assert r.classification.cls == "PERCEPTION_UNCERTAIN"
    assert any(e["type"] == "error" and e["data"]["where"] == "perception_ok" for e in events)


class ResettingWorld(GatedWorld):
    """Records when the runtime starts the gate window (WS3.6 `reset_gate()`)."""

    def __init__(self, *a, fail_reset=False, **kw):
        super().__init__(*a, **kw)
        self.fail_reset, self.resets = fail_reset, []

    def reset_gate(self):
        self.resets.append(self.observed)  # how many scenes were observed before the reset
        if self.fail_reset:
            raise RuntimeError("world model not running")


def test_the_runtime_starts_the_gate_window_once_per_attempt():
    world = _demo(ResettingWorld, rim_slip=True)
    r = _slip_on(world)
    assert world.resets == [0] and r.classification.cls == "SLIP_IN_CARRY"


def test_a_crashing_gate_reset_emits_an_error_and_the_attempt_still_runs(tmp_path):
    events = []
    bus = EventBus("reset-crash", tmp_path)
    bus.subscribe(events.append)
    world = _demo(ResettingWorld, rim_slip=True, fail_reset=True)
    r = _run(demo_sources()[0], CUPS, world=world, success=ON_TOP, skill="stack", bus=bus)
    bus.close()
    assert r.trace and r.classification.cls == "SLIP_IN_CARRY"
    assert any(e["type"] == "error" and e["data"]["where"] == "perception.reset_gate" for e in events)


def test_a_passing_gate_keeps_the_skill_label():
    r = _slip_on(_demo(GatedWorld, rim_slip=True, gate=(True, "markers within 2 mm")))
    assert r.classification.cls == "SLIP_IN_CARRY"


# ---- unknown bucket and confidence gating -------------------------------------------------

def test_an_unexplained_skill_error_is_unknown_with_low_confidence():
    r = _run(_skill("robot.move_to(0.2, 0.0, 0.18)", "raise SkillError('gave up')"))
    c = _expect(r, "UNKNOWN", "approach", 0)
    assert c.bucket == BUCKET_UNKNOWN and c.confidence < MIN_SKILL_CONFIDENCE and "gave up" in c.summary


def test_a_weak_skill_guess_is_reported_as_unknown_with_the_guess():
    r = _run(_skill("robot.note('thinking')"), success=ON_TOP)
    c = _expect(r, "UNKNOWN", "grasp", 0)
    assert c.evidence["guess"] == "NO_PICK" and "best guess NO_PICK" in c.summary
    assert c.confidence < MIN_SKILL_CONFIDENCE


# ---- runtime, events and the seeded benchmark ---------------------------------------------

def test_runtime_emits_phase_checks_then_the_classification(tmp_path):
    events = []
    bus = EventBus("classified", tmp_path)
    bus.subscribe(events.append)
    _run(demo_sources()[0], CUPS, rim_slip=True, success=ON_TOP, skill="stack", bus=bus)
    bus.close()
    kinds = [e["type"] for e in events]
    assert kinds.index("phase.check") < kinds.index("attempt.classified") < kinds.index("attempt.finished")
    (classified,) = [e["data"] for e in events if e["type"] == "attempt.classified"]
    assert classified["cls"] == "SLIP_IN_CARRY" and classified["phase"] == "carry"
    assert kinds[kinds.index("verify.result") - 1] == "scene.observed"  # the final scene, for replays


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_every_seeded_benchmark_bug_gets_its_class_and_phase(case):
    r = _run(case.source, CUPS, rim_slip=True, success=ON_TOP, skill="stack")
    c = r.classification
    assert (c.cls, c.phase) == (case.cls, case.phase), c
    assert c.bucket == BUCKET_SKILL and c.confidence >= MIN_SKILL_CONFIDENCE
    assert not [w for w in SLOT_WORDS if w in c.hint.lower()], c.hint


def _synthetic(error):
    return AttemptResult(ok=False, env="fake", skill="t", version=0, trace=[], error=error, verify=None,
                         safety=SafetyReport(ok=True), scene_before=None, scene_after=None, duration_s=30.2)


def test_numbers_are_finite_in_every_evidence_dict():
    for case in CASES:
        c = _run(case.source, CUPS, rim_slip=True, success=ON_TOP, skill="stack").classification
        for v in c.evidence.values():
            assert not isinstance(v, float) or math.isfinite(v), (case.name, c.evidence)
