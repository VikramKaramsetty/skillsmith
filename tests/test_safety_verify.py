import pytest

from skillsmith.config import load_config
from skillsmith.safety import SafetyGuard, audit_trace
from skillsmith.tasks import parse_task
from skillsmith.types import PrimitiveCall, SafetyViolation, Scene, SceneObject
from skillsmith.verify import evaluate

cfg = load_config()


def codes(p):
    return {i.code for i in SafetyGuard(cfg).target_issues(p)}


def test_envelope():
    assert codes((0.2, 0.0, 0.1)) == set()
    assert "OUT_OF_BOX" in codes((0.5, 0.0, 0.1))
    assert "TOO_LOW" in codes((0.2, 0.0, 0.001))
    assert "OUT_OF_REACH" in codes((0.30, 0.19, 0.1))
    assert "TOO_CLOSE" in codes((0.06, 0.0, 0.1))
    with pytest.raises(SafetyViolation):
        SafetyGuard(cfg).check_target((0.2, 0.0, -0.05))


def test_speed_clamped_and_budget():
    g = SafetyGuard(cfg)
    assert g.clamp_speed(10.0) == cfg.safety.max_tcp_speed
    assert g.clamp_speed(None) == cfg.safety.default_tcp_speed
    with pytest.raises(SafetyViolation):
        for _ in range(cfg.safety.max_calls + 1):
            g.count_call()


def test_audit_trace_flags_bad_target_and_collision():
    trace = [
        PrimitiveCall(0, "move_to", {"x": 0.2, "y": 0.0, "z": 0.1}, 0.0),
        PrimitiveCall(1, "move_to", {"x": 0.6, "y": 0.0, "z": 0.1}, 0.0),
        PrimitiveCall(2, "move_to", {"x": 0.2, "y": 0.0, "z": 0.05}, 0.0, ok=False,
                      error="CollisionError: held cup hit red cup"),
    ]
    report = audit_trace(trace, cfg)
    assert not report.ok
    assert {"OUT_OF_BOX", "COLLISION"} <= {i.code for i in report.issues}


def scene(*objs):
    return Scene(tuple(objs), 0.0, "test")


BLUE = SceneObject("b", "blue cup", "blue", "cup", 0.22, 0.08, 0.08, 0.08, 0.065)
RED = SceneObject("r", "red cup", "red", "cup", 0.22, 0.08, 0.0, 0.08, 0.065)


def test_on_top():
    assert evaluate({"op": "on_top", "args": ["blue cup", "red cup"]}, scene(BLUE, RED)).ok
    off = SceneObject("b", "blue cup", "blue", "cup", 0.22, -0.08, 0.0, 0.08, 0.065)
    r = evaluate({"op": "on_top", "args": ["blue cup", "red cup"]}, scene(off, RED))
    assert not r.ok and "16.0 cm" in r.detail


def test_missing_object_detail():
    r = evaluate({"op": "on_top", "args": ["green cup", "red cup"]}, scene(RED))
    assert not r.ok and "cannot see 'green cup'" in r.detail


def test_parse_task():
    t = parse_task("Stack the blue cup on the red cup.")
    assert (t.skill, t.params) == ("stack", {"top": "blue cup", "bottom": "red cup"})
    assert t.success == {"op": "on_top", "args": ["blue cup", "red cup"]}
    t = parse_task("put the red block in the green cup")
    assert t.skill == "put_in" and t.success["op"] == "inside"
    with pytest.raises(ValueError):
        parse_task("do a backflip")
