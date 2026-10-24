"""WS4.3: static carry-path check. A held object never sweeps through a known object.

The guard rejects the move before anything moves; the post-hoc audit flags it too. The message
names the held object, the obstacle, the move and the clearance in cm, plus the travel height
that would clear it, so Droid can fix the skill in one round.
"""

import pytest

from skillsmith.config import Config, load_config
from skillsmith.events import NullBus
from skillsmith.hal.base import Env
from skillsmith.hal.fake import FakeWorld
from skillsmith.safety import (
    CARRY_CODE,
    DEFAULT_CARRY_CLEARANCE,
    CarryTracker,
    HeldBody,
    SafetyGuard,
    audit_trace,
    carry_clearance,
    clearance,
    held_body,
    min_clearance,
)
from skillsmith.skills.runtime import run_skill
from skillsmith.types import PrimitiveCall, SafetyViolation, Scene, SceneObject

cfg = load_config()

BLUE = SceneObject("blue_cup_0", "blue cup", "blue", "cup", 0.22, -0.08, 0.0, 0.08, 0.065)
RED = SceneObject("red_cup_0", "red cup", "red", "cup", 0.22, 0.08, 0.0, 0.08, 0.065)
GREEN = SceneObject("green_block_0", "green block", "green", "block", 0.15, 0.0, 0.0, 0.03, 0.03)
SCENE = Scene((BLUE, RED, GREEN), 0.0, "test")
GRIP = (0.22, -0.08, 0.04)  # tip at the blue cup's mid-height: its base hangs 4 cm below the tip

CARRY = '''SKILL = {{"name": "carry", "params": {{}}, "uses": []}}


def run(robot, scene, skills):
    a = scene.require("blue cup")
    b = scene.require("red cup")
    robot.release()
    robot.move_to(a.x, a.y, 0.18)
    robot.move_to(a.x, a.y, a.z + a.height * 0.5)
    if not robot.grasp():
        raise SkillError("missed")
    robot.move_to(a.x, a.y, {travel})
    robot.move_to(b.x, b.y, {travel})
'''


def fake_env():
    w = FakeWorld.demo(cfg)
    return Env("fake", w, w, w)


def carrying_guard(margin_cfg=cfg):
    g = SafetyGuard(margin_cfg)
    g.check_carry(GRIP, (0.22, -0.08, 0.18), 0, True, SCENE)  # grasp seen: lift straight up
    return g


# ---- geometry ----------------------------------------------------------------------------------
def test_clearance_is_signed_distance_between_cylinders():
    held = held_body(SCENE, GRIP)
    assert (held.id, held.radius, held.height) == ("blue_cup_0", 0.0325, 0.08)
    assert held.offset == pytest.approx(-0.04)
    above = clearance((0.22, 0.08, 0.14), held, RED)  # held base 2 cm above the red rim
    assert above == pytest.approx(0.02)
    through = clearance((0.22, 0.08, 0.09), held, RED)  # base at 5 cm, rim at 8 cm
    assert through == pytest.approx(-0.03)
    beside = clearance((0.22, 0.0, 0.09), held, RED)  # 8 cm apart, radii 3.25 + 3.25
    assert beside == pytest.approx(0.015)


def test_min_clearance_finds_the_middle_of_a_segment():
    held = held_body(SCENE, GRIP)
    start, end = (0.22, -0.08, 0.07), (0.10, 0.08, 0.07)
    c, t = min_clearance(start, end, held, GREEN)
    assert clearance(start, held, GREEN) > 0.05 and clearance(end, held, GREEN) > 0.03  # both ends clear
    assert c < 0.005 and 0.2 < t < 0.8  # the middle of the move passes over the block


def test_unknown_held_object_is_a_point_at_the_tip():
    body = held_body(SCENE, (0.30, 0.0, 0.10))
    assert body == HeldBody(None, "the held object", 0.0, 0.0, 0.0)


# ---- guard: rejects before motion --------------------------------------------------------------
def test_guard_rejects_low_carry_and_names_object_move_and_cm():
    g = carrying_guard()
    with pytest.raises(SafetyViolation) as e:
        g.check_carry((0.22, -0.08, 0.09), (0.22, 0.08, 0.09), 7, True, SCENE)
    msg = str(e.value)
    assert msg.startswith(f"[{CARRY_CODE}] carrying blue cup")
    assert "(0.220, -0.080, 0.090) -> (0.220, 0.080, 0.090) (call 7)" in msg
    assert "sweeps through red cup: clearance -3.0 cm < 1.0 cm required" in msg
    assert "tip is at z >= 0.130 over red cup" in msg  # rim 0.08 + 4 cm hang + 1 cm margin


def test_guard_catches_an_object_crossed_mid_segment():
    g = carrying_guard()
    with pytest.raises(SafetyViolation, match="green block: clearance 0.0 cm"):
        g.check_carry((0.22, -0.08, 0.07), (0.10, 0.08, 0.07), 5, True, SCENE)


def test_guard_allows_high_carry_lift_off_support_and_place_descent():
    g = carrying_guard()
    g.check_carry((0.22, -0.08, 0.18), (0.22, 0.08, 0.18), 3, True, SCENE)    # base 6 cm over the rim
    g.check_carry((0.22, 0.08, 0.18), (0.22, 0.08, 0.125), 4, True, SCENE)    # lowering onto red cup
    on_cup = SceneObject("g", "green block", "green", "block", 0.22, 0.08, 0.08, 0.03, 0.03)
    stacked = Scene((RED, on_cup), 0.0, "test")
    lift = SafetyGuard(cfg)
    lift.check_carry((0.22, 0.08, 0.095), (0.22, 0.08, 0.18), 0, True, stacked)  # resting on the cup
    assert lift.carry.held.id == "g"


def test_guard_ignores_moves_when_not_holding():
    SafetyGuard(cfg).check_carry((0.22, -0.08, 0.05), (0.22, 0.08, 0.05), 0, False, SCENE)


def test_margin_comes_from_config_with_a_default():
    assert carry_clearance(cfg) == DEFAULT_CARRY_CLEARANCE or "carry_clearance" in cfg.raw()["safety"]
    wide = Config({**cfg.raw(), "safety": {**cfg.raw()["safety"], "carry_clearance": 0.07}})
    assert carry_clearance(wide) == 0.07
    g = carrying_guard(wide)
    with pytest.raises(SafetyViolation, match="passes too close to red cup: clearance 6.0 cm < 7.0 cm"):
        g.check_carry((0.22, -0.08, 0.18), (0.22, 0.08, 0.18), 3, True, SCENE)


def test_released_object_is_tracked_without_a_new_look():
    t = CarryTracker(0.01)
    t.update(GRIP, True, SCENE)
    t.update((0.30, 0.0, 0.045), False, SCENE)  # put the blue cup down at (0.30, 0.0)
    assert t.held is None
    t.update((0.22, 0.08, 0.04), True, SCENE)    # now carry the red cup low past both spots
    assert t.held.id == "red_cup_0"
    old_spot = t.segment_issues((0.22, 0.03, 0.10), (0.22, -0.12, 0.10), 9)
    new_spot = t.segment_issues((0.20, 0.02, 0.10), (0.32, 0.02, 0.10), 9)
    assert old_spot == []
    assert len(new_spot) == 1 and "blue cup" in new_spot[0].message


# ---- end to end on the fake world -------------------------------------------------------------
def test_low_travel_is_stopped_before_motion_and_audited():
    env = fake_env()
    r = run_skill(CARRY.format(travel="0.09"), env, {}, cfg, NullBus(), skill="carry")
    assert not r.ok
    assert r.error.startswith(f"SafetyViolation: [{CARRY_CODE}] carrying blue cup")
    assert "red cup" in r.error and "cm" in r.error and "carry.py line 13" in r.error
    stopped = r.trace[-1]
    assert stopped.name == "move_to" and not stopped.ok
    assert stopped.ee_after == stopped.ee_before == (0.22, -0.08, 0.09)  # nothing moved
    red = r.scene_after.find("red cup")
    assert (red.x, red.y, red.z) == (RED.x, RED.y, RED.z)
    assert [i.code for i in r.safety.issues] == [CARRY_CODE]
    assert r.safety.issues[0].call_index == stopped.index


def test_raising_the_travel_height_fixes_it():
    r = run_skill(CARRY.format(travel="0.18"), fake_env(), {}, cfg, NullBus(), skill="carry")
    assert r.ok, r.reason()
    assert r.safety.ok


# ---- post-hoc audit ------------------------------------------------------------------------
def _trace(scene_in_look=True):
    calls = [PrimitiveCall(0, "look", {}, 0.0, (0.18, 0.0, 0.15), (0.18, 0.0, 0.15), result=SCENE)]
    if not scene_in_look:
        calls = []
    calls += [
        PrimitiveCall(1, "move_to", {"x": 0.22, "y": -0.08, "z": 0.04}, 0.0, (0.18, 0.0, 0.15), GRIP),
        PrimitiveCall(2, "grasp", {}, 0.0, GRIP, GRIP, result=True),
        PrimitiveCall(3, "move_to", {"x": 0.22, "y": -0.08, "z": 0.09}, 0.0, GRIP, (0.22, -0.08, 0.09)),
        PrimitiveCall(4, "move_to", {"x": 0.22, "y": 0.08, "z": 0.09}, 0.0, (0.22, -0.08, 0.09),
                      (0.22, 0.0, 0.09), ok=False, error="CollisionError: held blue cup hit red cup"),
        PrimitiveCall(5, "release", {}, 0.0, (0.22, 0.0, 0.09), (0.22, 0.0, 0.09)),
        PrimitiveCall(6, "move_to", {"x": 0.22, "y": 0.08, "z": 0.05}, 0.0, (0.22, 0.0, 0.09),
                      (0.22, 0.08, 0.05)),
    ]
    return calls


def test_audit_rechecks_carry_segments_from_the_trace():
    report = audit_trace(_trace(), cfg)
    carry = [i for i in report.issues if i.code == CARRY_CODE]
    assert len(carry) == 1 and carry[0].call_index == 4  # the move after release is not a carry
    assert "carrying blue cup" in carry[0].message and "red cup" in carry[0].message
    assert {"COLLISION", CARRY_CODE} <= {i.code for i in report.issues}


def test_audit_uses_the_scene_before_when_no_look_was_recorded():
    assert not any(i.code == CARRY_CODE for i in audit_trace(_trace(False), cfg).issues)
    report = audit_trace(_trace(False), cfg, scene=SCENE)
    assert [i.call_index for i in report.issues if i.code == CARRY_CODE] == [4]


def test_audit_reports_a_guard_rejection_as_carry_path_not_envelope():
    err = "SafetyViolation: [CARRY_PATH] carrying blue cup, move ... sweeps through red cup"
    trace = [PrimitiveCall(0, "move_to", {"x": 0.22, "y": 0.08, "z": 0.09}, 0.0, (0.22, -0.08, 0.09),
                           (0.22, -0.08, 0.09), ok=False, error=err)]
    report = audit_trace(trace, cfg, scene=SCENE)
    assert [(i.code, i.call_index) for i in report.issues] == [(CARRY_CODE, 0)]
