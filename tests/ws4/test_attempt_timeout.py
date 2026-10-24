"""WS4.2: a wall-clock limit per attempt stops the arm and names the skill line that was running."""

import copy
import re
import sys
import time
from contextlib import nullcontext

import pytest

from skillsmith.config import Config, load_config
from skillsmith.events import NullBus
from skillsmith.hal.base import Env
from skillsmith.hal.fake import FakeWorld
from skillsmith.skills import runtime
from skillsmith.skills.runtime import DEFAULT_ATTEMPT_TIMEOUT_S, AttemptTimeout, attempt_timeout_s, run_skill
from skillsmith.skills.validator import validate

BASE = load_config()
HEADER = 'SKILL = {"name": "t", "params": {}, "uses": []}\n\n'


def skill(body: str) -> str:
    return HEADER + "def run(robot, scene, skills):\n" + "".join(f"    {line}\n" for line in body.splitlines())


# 50**5 iterations of pure Python: passes the validator, never calls the robot, runs for minutes.
SPIN = skill(
    'robot.note("spinning")\n'
    "total = 0\n"
    "for a in range(50):\n"
    "    for b in range(50):\n"
    "        for c in range(50):\n"
    "            for d in range(50):\n"
    "                for e in range(50):\n"
    "                    total = total + 1\n"
    'robot.note(f"never {total}")'
)
SPIN_LINE = r"(total = total \+ 1|for [a-e] in range\(50\):)"
LOOKS = skill("for i in range(50):\n    robot.look()")


def with_timeout(seconds) -> Config:
    data = copy.deepcopy(BASE.raw())
    data["safety"]["attempt_timeout_s"] = seconds
    return Config(data)


def fake_env():
    w = FakeWorld.demo(BASE)
    stops = []
    w.stop = lambda: stops.append(time.monotonic())
    return Env("fake", w, w, w), w, stops


def run(source, cfg, bus=None, library=None):
    env, world, stops = fake_env()
    t0 = time.monotonic()
    result = run_skill(source, env, {}, cfg, bus or NullBus(), skill="t", library=library)
    return result, stops, time.monotonic() - t0, world


def test_spin_skill_is_legal_so_only_the_timeout_can_stop_it():
    assert validate(SPIN) == []


def test_pure_python_loop_times_out_stops_arm_and_names_the_line():
    bus = NullBus()
    trace_before = sys.gettrace()
    r, stops, wall, _ = run(SPIN, with_timeout(0.2), bus)

    assert not r.ok
    assert r.error.startswith("AttemptTimeout: attempt ran ")
    assert "0.2 s wall-clock limit" in r.error
    assert re.search(r"\(t\.py line \d+: " + SPIN_LINE + r"\)$", r.error), r.error
    assert len(stops) == 1  # env.arm.stop() was called
    assert wall < 5
    assert [c.args.get("text") for c in r.trace] == ["spinning"]  # nothing ran after the limit
    assert sys.gettrace() is trace_before  # the line tracer is removed again

    errors = [e["data"] for e in bus.events if e["type"] == "error"]
    assert errors == [{"where": "attempt.timeout", "message": r.error}]
    finished = [e["data"] for e in bus.events if e["type"] == "attempt.finished"]
    assert finished[0]["ok"] is False and finished[0]["error"] == r.error


def test_primitive_check_alone_stops_a_slow_skill(monkeypatch):
    # Without the line tracer, the Robot's own check still refuses the next primitive.
    monkeypatch.setattr(runtime, "_line_deadline", lambda deadline: nullcontext())
    env, world, stops = fake_env()
    observe = world.observe

    def slow_observe():
        time.sleep(0.03)
        return observe()

    world.observe = slow_observe
    r = run_skill(LOOKS, env, {}, with_timeout(0.15), NullBus(), skill="t")

    assert r.error.startswith("AttemptTimeout")
    assert r.error.endswith("(t.py line 5: robot.look())")
    assert 0 < len(r.trace) < 50
    assert all(c.ok for c in r.trace)  # the refused call never started, so it is not in the trace
    assert len(stops) == 1


def test_timeout_inside_a_nested_skill_names_that_skill():
    outer = skill('skills.call("spin")')
    r, stops, _, _ = run(outer, with_timeout(0.2), library={"spin": SPIN})

    assert r.error.startswith("AttemptTimeout")
    assert re.search(r"\(spin\.py line \d+: " + SPIN_LINE + r"\)$", r.error), r.error
    assert len(stops) == 1


def test_quick_skill_is_untouched_by_the_limit():
    trace_before = sys.gettrace()
    r, stops, _, _ = run(skill('robot.note("hi")\nrobot.home()'), with_timeout(30))

    assert r.error is None
    assert [c.name for c in r.trace] == ["note", "home"]
    assert stops == []
    assert sys.gettrace() is trace_before


def test_other_errors_still_stop_the_arm_without_a_timeout_event():
    bus = NullBus()
    r, stops, _, _ = run(skill('raise SkillError("nope")'), with_timeout(30), bus)

    assert r.error.startswith("SkillError: nope (t.py line 4:")
    assert len(stops) == 1
    assert not [e for e in bus.events if e["type"] == "error"]


@pytest.mark.parametrize("value,expected", [
    (5, 5.0), (0.5, 0.5), (None, DEFAULT_ATTEMPT_TIMEOUT_S), (0, DEFAULT_ATTEMPT_TIMEOUT_S),
    (-1, DEFAULT_ATTEMPT_TIMEOUT_S), ("10", DEFAULT_ATTEMPT_TIMEOUT_S), (True, DEFAULT_ATTEMPT_TIMEOUT_S),
])
def test_timeout_comes_from_config_with_a_safe_default(value, expected):
    assert attempt_timeout_s(with_timeout(value)) == expected


def test_missing_config_key_uses_the_default():
    data = copy.deepcopy(BASE.raw())
    data["safety"].pop("attempt_timeout_s", None)
    assert attempt_timeout_s(Config(data)) == DEFAULT_ATTEMPT_TIMEOUT_S


def test_deadline_only_counts_after_begin():
    now = [100.0]
    deadline = runtime._Deadline(1.0, clock=lambda: now[0])
    now[0] = 500.0
    deadline.check()  # not started yet
    deadline.begin()
    now[0] = 500.9
    deadline.check()
    now[0] = 501.1
    with pytest.raises(AttemptTimeout, match="ran 1.10 s, over the 1 s wall-clock limit"):
        deadline.check()
