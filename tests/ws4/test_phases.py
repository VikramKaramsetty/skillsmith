"""WS4.4: phase segmentation of a trace, one check per phase, on the fake world."""

from skillsmith.analysis.phases import segment_phases
from skillsmith.config import REPO_ROOT, load_config
from skillsmith.demo import demo_sources
from skillsmith.events import NullBus
from skillsmith.hal.base import Env
from skillsmith.hal.fake import FakeWorld
from skillsmith.skills.runtime import run_skill
from skillsmith.types import PHASES, AttemptResult, PrimitiveCall, SafetyReport

cfg = load_config()
PICK_PLACE = (REPO_ROOT / "skills/library/pick_place.py").read_text()
LIBRARY = {"pick_place": PICK_PLACE}
STACK = {"top": "blue cup", "bottom": "red cup"}


def _run(source, params, rim_slip=False, skill="stack"):
    w = FakeWorld.demo(cfg, rim_slip=rim_slip)
    return run_skill(source, Env("fake", w, w, w), params, cfg, NullBus(), skill=skill, library=LIBRARY)


def _by_phase(attempt):
    return {c.phase: c for c in segment_phases(attempt, cfg)}


def _assert_covers(checks, trace):
    """In PHASES order, contiguous, and ending on the last call."""
    assert [c.phase for c in checks] == sorted((c.phase for c in checks), key=PHASES.index)
    for prev, nxt in zip(checks, checks[1:]):
        assert nxt.start_index == prev.end_index + 1
    assert checks[-1].end_index == trace[-1].index


def test_successful_stack_has_all_seven_phases_and_all_pass():
    v1, _ = demo_sources()
    r = _run(v1, STACK)
    assert r.ok, r.reason()
    checks = segment_phases(r, cfg)
    assert [c.phase for c in checks] == list(PHASES)
    assert all(c.ok for c in checks), checks
    assert checks[0].start_index == 0  # the opening note and release belong to the approach
    _assert_covers(checks, r.trace)


def test_phase_details_carry_numbers():
    v1, _ = demo_sources()
    phases = _by_phase(_run(v1, STACK))
    assert phases["approach"].detail == "ended 0.0 cm off blue cup centre, 1.0 cm below its top"
    assert phases["grasp"].detail == "closed at z=0.070 = 88% of blue cup height, holding"
    assert phases["lift"].detail == "lifted blue cup 11.0 cm to z=0.180"
    assert phases["carry"].detail == "blue cup held through 16.0 cm sideways move"
    assert phases["place"].detail == "lowered 2.5 cm to z=0.155"
    assert phases["release"].detail.startswith("opened at z=0.155; blue cup settled 0.0 cm")


def test_rim_slip_fails_the_carry_phase_and_stops_there():
    v1, _ = demo_sources()
    r = _run(v1, STACK, rim_slip=True)
    assert not r.ok and "lost blue cup" in r.error
    checks = segment_phases(r, cfg)
    assert [c.phase for c in checks] == ["approach", "grasp", "lift", "carry"]
    assert [c.ok for c in checks] == [True, True, True, False]
    carry = checks[-1]
    assert carry.detail == "lost blue cup during 16.0 cm sideways move (16.0 cm from the gripper)"
    assert carry.start_index == carry.end_index == r.trace[-1].index
    _assert_covers(checks, r.trace)


def test_nested_skill_calls_segment_the_same_way():
    _, v2 = demo_sources()
    r = _run(v2, STACK, rim_slip=True)
    assert r.ok, r.reason()
    phases = _by_phase(r)
    assert list(phases) == list(PHASES) and all(c.ok for c in phases.values())
    assert phases["grasp"].detail == "closed at z=0.040 = 50% of blue cup height, holding"
    assert phases["retreat"].end_index == r.trace[-1].index  # pick_place's retreat plus stack's home


def test_grasp_miss_fails_the_grasp_phase():
    miss = PICK_PLACE.replace("target.x, target.y, target.z + grip_offset", "target.x + 0.06, target.y, 0.04")
    r = _run(miss, {"obj": "blue cup", "x": 0.15, "y": 0.12}, skill="pick_place")
    assert not r.ok and "grasp missed" in r.error
    checks = segment_phases(r, cfg)
    assert [c.phase for c in checks] == ["approach", "grasp"]
    grasp = checks[-1]
    assert not grasp.ok
    assert "grasp missed blue cup" in grasp.detail  # the stop reason wins over the measured check


def test_collision_while_placing_fails_the_place_phase_at_the_failing_call():
    bad = PICK_PLACE.replace("place_z + grip_offset + 0.005", "place_z + 0.01")
    r = _run(bad, {"obj": "blue cup", "x": 0.22, "y": 0.08}, skill="pick_place")
    assert r.error.startswith("CollisionError")
    checks = segment_phases(r, cfg)
    place = checks[-1]
    failing = next(c for c in r.trace if not c.ok)
    assert place.phase == "place" and not place.ok
    assert place.end_index == failing.index
    assert "CollisionError" in place.detail and "with blue cup" in place.detail


def _call(i, name, before, after, result=None, ok=True, error=None):
    return PrimitiveCall(index=i, name=name, args={}, t=0.0, ee_before=before, ee_after=after,
                         ok=ok, result=result, error=error)


def _attempt(trace, error=None, scene_before=None, scene_after=None):
    return AttemptResult(ok=error is None, env="fake", skill="t", version=0, trace=trace, error=error,
                         verify=None, safety=SafetyReport(ok=True), scene_before=scene_before,
                         scene_after=scene_after, duration_s=0.0)


def test_empty_trace_has_no_phases():
    assert segment_phases(_attempt([], error="SkillError: 'cup' not found"), cfg) == []


def test_no_grasp_is_all_approach():
    a, b = (0.2, 0.0, 0.18), (0.2, 0.1, 0.18)
    checks = segment_phases(_attempt([_call(0, "move_to", a, b), _call(1, "home", b, a)]), cfg)
    assert [(c.phase, c.ok, c.start_index, c.end_index) for c in checks] == [("approach", True, 0, 1)]


def test_failed_grasp_retry_then_success_is_one_grasp_phase():
    hi, lo = (0.2, 0.0, 0.18), (0.2, 0.0, 0.04)
    trace = [_call(0, "move_to", hi, lo), _call(1, "grasp", lo, lo, result=False),
             _call(2, "move_to", lo, lo), _call(3, "grasp", lo, lo, result=True),
             _call(4, "move_to", lo, hi)]
    phases = {c.phase: c for c in segment_phases(_attempt(trace), cfg)}
    assert (phases["grasp"].start_index, phases["grasp"].end_index) == (1, 3)
    assert "after 2 tries" in phases["grasp"].detail
    assert (phases["lift"].start_index, phases["lift"].end_index) == (4, 4)


def test_only_the_last_pick_cycle_is_segmented():
    hi, lo, far_hi, far_lo = (0.2, 0.0, 0.18), (0.2, 0.0, 0.04), (0.2, 0.1, 0.18), (0.2, 0.1, 0.04)
    cycle = [("move_to", hi, lo), ("grasp", lo, lo), ("move_to", lo, hi), ("move_to", hi, far_hi),
             ("move_to", far_hi, far_lo), ("release", far_lo, far_lo), ("move_to", far_lo, far_hi),
             ("move_to", far_hi, hi)]
    trace = [_call(i, n, a, b, result=True if n == "grasp" else None) for i, (n, a, b) in enumerate(cycle * 2)]
    checks = segment_phases(_attempt(trace), cfg)
    assert [c.phase for c in checks] == list(PHASES)
    assert checks[0].start_index == 6  # the first cycle's retreat leads into the second approach
    assert checks[-1].end_index == 15
