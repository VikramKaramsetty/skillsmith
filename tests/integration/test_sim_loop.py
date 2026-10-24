"""QA.2: the full repair loop with the MuJoCo sim env as the check (and target) env.

`make demo-fake` proves the loop with a FakeWorld standing in for sim. On stage the gates run
in MuJoCo, so this file drives the same RepairLoop with `make_env("sim", ...)` and checks the
seams between WS2 (sim), WS4 (runtime, safety, analysis) and WS5 (loop, reports):

- the demo story: rim-grip v1 passes the sim gates, slips on the "real" arm (a rim-slip
  FakeWorld, since agents never touch hardware), the repair v2 passes sim and succeeds;
- sim as the target: a drafted skill goes gates → approval → success, all in MuJoCo;
- a sim contact (CollisionError) fails the sim/safety gates and comes back to the author as
  the repair report, before any approval is asked for;
- every event of those runs conforms to INTERFACES §7 (the QA.1 checker);
- the sim attempt carries a replay video and the dashboard gets `sim.video` (WS2.5).

Every scenario runs against two check backends: `fake` (a control that runs today, so the
test logic itself is proven) and `sim` (skipped until WS2.4 lands make_sim_env). When sim
fails and fake passes, the bug lives in the sim backend or in how the loop uses it.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Optional

import pytest
from qa_extras import built
from test_event_schema import _analysis_built, _cfg, _unexpected, _violations

from skillsmith.config import load_config
from skillsmith.demo import demo_sources, temp_library
from skillsmith.events import EventBus, NullBus
from skillsmith.hal.base import Env, make_env
from skillsmith.hal.fake import FakeWorld
from skillsmith.repair.approvers import AutoApprover
from skillsmith.repair.loop import RepairLoop, RunResult
from skillsmith.repair.scripted import ScriptedAuthor
from skillsmith.skills.library import SkillLibrary
from skillsmith.tasks import parse_task

TASK = "stack the blue cup on the red cup"

# Closes the gripper over the red cup, then pushes the closed, empty gripper into it: an
# unintended contact the check env must catch (fake: CollisionError; sim: contact → CollisionError
# plus a SafetyIssue). Every target is inside the workspace, so the SafetyGuard lets it through.
COLLIDING = '''"""stack: deliberately pokes the bottom cup with a closed gripper."""

SKILL = {"name": "stack", "params": {"top": "str", "bottom": "str"}, "uses": []}


def run(robot, scene, skills, top, bottom):
    b = scene.require(bottom)
    robot.move_to(b.x, b.y, 0.18)
    robot.grasp()
    robot.move_to(b.x, b.y, b.z + b.height / 2)
    robot.home()
'''


@functools.cache
def _sim_error() -> Optional[Exception]:
    """None if make_sim_env builds an Env, else why not."""
    try:
        env = make_env("sim", load_config())
    except (NotImplementedError, ImportError) as e:
        return e
    env.close()
    return None


def _check_env(backend: str, cfg):
    if backend == "sim":
        built(_sim_error(), "sim", "waiting on WS2.4 (make_sim_env)")
    return lambda scene: make_env(backend, cfg, scene)


@pytest.fixture(params=["fake", "sim"])
def backend(request) -> str:
    return request.param


def _loop(cfg, target: Env, make_check, author, bus: EventBus) -> RepairLoop:
    return RepairLoop(cfg, target, make_check, SkillLibrary(temp_library()), author,
                      AutoApprover(target.name), bus)


def _stage_target(cfg) -> Env:
    """The 'real' arm for the demo story: a FakeWorld where a rim grip slips (no hardware)."""
    world = FakeWorld.demo(cfg, rim_slip=True)
    return Env("fake", world, world, world)


def _of(bus: NullBus, kind: str) -> list[dict]:
    return [e["data"] for e in bus.events if e["type"] == kind]


def _demo_story(cfg, backend: str) -> tuple[RunResult, NullBus, ScriptedAuthor]:
    v1, v2 = demo_sources()
    author, bus = ScriptedAuthor([v1], [v2]), NullBus()
    loop = _loop(cfg, _stage_target(cfg), _check_env(backend, cfg), author, bus)
    return loop.run(parse_task(TASK)), bus, author


# ---- the demo story with sim gates ----------------------------------------------------------

def test_demo_story_gates_in_check_env_then_repairs(tmp_path, backend):
    result, bus, author = _demo_story(_cfg(tmp_path), backend)

    assert result.ok and result.rounds == 1, [e["data"] for e in bus.events if e["type"] == "attempt.finished"]
    attempts = [(d["env"], d["ok"]) for d in _of(bus, "attempt.finished")]
    assert attempts == [(backend, True), ("fake", False), (backend, True), ("fake", True)]
    gates = [(d["gate"], d["ok"]) for d in _of(bus, "gate.result")]
    assert gates == [("static", True), ("sim", True), ("safety", True)] * 2
    assert "lost blue cup" in author.reports[0]  # the real failure reached the author
    assert [d["round"] for d in _of(bus, "repair.started")] == [1]
    assert _of(bus, "skill.saved")[0]["skill"] == "stack"


def test_repair_report_carries_the_sim_run_as_expected(tmp_path, backend):
    """The passing sim run of v1 is the 'expected' half of expected-vs-actual for Droid."""
    v1, v2 = demo_sources()
    cfg, bus = _cfg(tmp_path), NullBus()
    seen = []

    class Recording(ScriptedAuthor):
        def repair(self, task, source, report_md, ctx):
            seen.append((ctx.attempt, ctx.expected))
            return super().repair(task, source, report_md, ctx)

    loop = _loop(cfg, _stage_target(cfg), _check_env(backend, cfg), Recording([v1], [v2]), bus)
    assert loop.run(parse_task(TASK)).ok
    ((attempt, expected),) = seen
    assert attempt.env == "fake" and not attempt.ok
    assert expected is not None and expected.env == backend and expected.ok


# ---- sim as the target ----------------------------------------------------------------------

def test_draft_runs_gates_approval_and_succeeds_on_check_backend(tmp_path, backend):
    _, v2 = demo_sources()
    cfg, bus = _cfg(tmp_path), NullBus()
    make_check = _check_env(backend, cfg)
    target = make_env(backend, cfg)
    try:
        result = _loop(cfg, target, make_check, ScriptedAuthor([v2], []), bus).run(parse_task(TASK))
    finally:
        target.close()

    assert result.ok and result.rounds == 0
    assert [d["env"] for d in _of(bus, "attempt.finished")] == [backend, backend]
    assert [d["ok"] for d in _of(bus, "verify.result")] == [True, True]
    types = bus.types()
    assert types.index("gate.result") < types.index("approval.requested") < types.index("skill.saved")


# ---- a sim contact goes back to the author --------------------------------------------------

def test_contact_in_check_env_fails_gates_and_feeds_the_author(tmp_path, backend):
    _, v2 = demo_sources()
    cfg, bus = _cfg(tmp_path), NullBus()
    author = ScriptedAuthor([COLLIDING], [v2])
    result = _loop(cfg, _stage_target(cfg), _check_env(backend, cfg), author, bus).run(parse_task(TASK))

    assert result.ok and result.rounds == 1
    sim_gates = [d for d in _of(bus, "gate.result") if d["gate"] in ("sim", "safety")]
    assert not (sim_gates[0]["ok"] and sim_gates[1]["ok"]), sim_gates[:2]
    assert "CollisionError" in author.reports[0], author.reports[0]
    # The colliding draft never reached approval or the target arm.
    assert len(_of(bus, "approval.requested")) == 1
    assert [d["env"] for d in _of(bus, "attempt.finished")] == [backend, backend, "fake"]


def test_sim_contact_is_also_a_safety_issue(tmp_path):
    """INTERFACES §2: sim exposes contacts via arm.safety_issues(), so the safety gate fails too."""
    _, v2 = demo_sources()
    cfg, bus = _cfg(tmp_path), NullBus()
    author = ScriptedAuthor([COLLIDING], [v2])
    _loop(cfg, _stage_target(cfg), _check_env("sim", cfg), author, bus).run(parse_task(TASK))
    safety = next(d for d in _of(bus, "gate.result") if d["gate"] == "safety")
    assert not safety["ok"], safety


# ---- events and visibility ------------------------------------------------------------------

def test_events_conform_to_interfaces(tmp_path, backend):
    cfg = _cfg(tmp_path)
    _, bus, _ = _demo_story(cfg, backend)
    _, v2 = demo_sources()
    collide_bus = NullBus()
    _loop(cfg, _stage_target(cfg), _check_env(backend, cfg), ScriptedAuthor([COLLIDING], [v2]),
          collide_bus).run(parse_task(TASK))
    for events in (bus.events, collide_bus.events):
        assert _unexpected(_violations(events)) == []


@pytest.mark.skipif(not _analysis_built(), reason="waiting on WS4.4/WS4.5 (analyse_attempt)")
def test_sim_attempts_are_analysed(tmp_path, backend):
    _, bus, _ = _demo_story(_cfg(tmp_path), backend)
    assert {"phase.check", "attempt.classified"} <= set(bus.types())


def test_sim_gate_emits_a_replay_video(tmp_path):
    cfg = _cfg(tmp_path)
    make_check = _check_env("sim", cfg)
    probe = make_check(FakeWorld.demo(cfg).observe())
    try:
        renders = hasattr(probe.arm, "last_video_path")
    finally:
        probe.close()
    if not renders:
        pytest.skip("waiting on WS2.5 (arm.last_video_path)")
    result, bus, _ = _demo_story(cfg, "sim")
    assert result.ok
    videos = [d["path"] for d in _of(bus, "sim.video")]
    assert len(videos) == 2 and all(Path(p).is_file() for p in videos), videos
