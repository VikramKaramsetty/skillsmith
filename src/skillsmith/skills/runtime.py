"""Run a skill: validate, exec with stripped builtins, record the trace, verify, audit.

`run_skill` is the single entry point used by the CLI, the repair loop and tests.

Every attempt has a wall-clock limit (`safety.attempt_timeout_s`, WS4.2). It is enforced in the
skill's own thread, so nothing is left running: the Robot checks it before each primitive call,
and a line tracer on the skill's frames checks it between lines, which catches pure-Python loops
that never call the robot. Either way an `AttemptTimeout` propagates, the arm is stopped, an
`error` event is emitted and the report names the skill file and line that was running.
"""

from __future__ import annotations

import builtins
import dataclasses
import math
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from skillsmith.events import EventBus
from skillsmith.primitives import Robot, SkillCaller
from skillsmith.safety import SafetyGuard, audit_trace
from skillsmith.skills.validator import ALLOWED_BUILTINS, validate
from skillsmith.types import AttemptResult, Scene, SkillError
from skillsmith.verify import evaluate

SAFE_BUILTINS: Dict[str, Any] = {name: getattr(builtins, name) for name in ALLOWED_BUILTINS}
SAFE_BUILTINS["SkillError"] = SkillError

DEFAULT_ATTEMPT_TIMEOUT_S = 120.0  # used when config/robot.yaml has no safety.attempt_timeout_s
_SKILL_FILE_PREFIX = "<skill:"


class AttemptTimeout(SkillError):
    """The attempt ran past its wall-clock limit; the runtime stops the arm (class TIMEOUT)."""


class _Deadline:
    """Wall-clock budget for one attempt. `check()` raises AttemptTimeout once it has run out."""

    def __init__(self, limit_s: float, clock: Callable[[], float] = time.monotonic):
        self.limit_s = limit_s
        self._clock = clock
        self._start: Optional[float] = None

    def begin(self) -> None:
        self._start = self._clock()

    def check(self) -> None:
        if self._start is None:
            return
        elapsed = self._clock() - self._start
        if elapsed > self.limit_s:
            raise AttemptTimeout(f"attempt ran {elapsed:.2f} s, over the {self.limit_s:g} s wall-clock limit "
                                 "(safety.attempt_timeout_s); the arm was stopped")


class _TimedRobot(Robot):
    """`robot` with the cooperative timeout check: no primitive starts after the deadline."""

    def __init__(self, env, guard: SafetyGuard, bus: EventBus, cfg, deadline: _Deadline):
        super().__init__(env, guard, bus, cfg)
        self._deadline = deadline

    def _record(self, name: str, args: dict, fn: Callable[[int], Any]) -> Any:
        self._deadline.check()
        return super()._record(name, args, fn)


def attempt_timeout_s(cfg) -> float:
    """`safety.attempt_timeout_s` from the config, or the default when it is missing or not positive."""
    value = getattr(getattr(cfg, "safety", None), "attempt_timeout_s", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return DEFAULT_ATTEMPT_TIMEOUT_S
    return float(value)


@contextmanager
def _line_deadline(deadline: _Deadline) -> Iterator[None]:
    """Hard backstop: check the deadline before every line of skill code (frames from
    `<skill:...>` only; the runtime, primitives and env run untraced). The previous trace
    function (debugger, coverage) is restored afterwards; it is not chained, because a C tracer
    called from Python reinstalls itself and would silently remove this check."""
    def on_line(frame, event, arg):
        if event == "line":
            deadline.check()
        return on_line

    def on_call(frame, event, arg):
        return on_line if frame.f_code.co_filename.startswith(_SKILL_FILE_PREFIX) else None

    previous = sys.gettrace()
    sys.settrace(on_call)
    try:
        yield
    finally:
        sys.settrace(previous)


class SkillValidationError(SkillError):
    def __init__(self, name: str, issues):
        self.issues = issues
        super().__init__(f"{name} failed static checks: " + "; ".join(str(i) for i in issues))


def compile_skill(source: str, name: str) -> dict:
    issues = validate(source)
    if issues:
        raise SkillValidationError(name, issues)
    namespace = {"__builtins__": SAFE_BUILTINS, "math": math, "SkillError": SkillError}
    exec(compile(source, f"<skill:{name}>", "exec"), namespace)
    return namespace


def _invoke(source: str, name: str, robot: Robot, scene: Scene, skills: SkillCaller, params: dict):
    return compile_skill(source, name)["run"](robot, scene, skills, **params)


def _describe_error(e: BaseException, sources: Dict[str, str]) -> str:
    """'CollisionError: ... (stack.py line 14: robot.move_to(...))' — the line helps Droid a lot."""
    where = ""
    for frame in reversed(traceback.extract_tb(e.__traceback__)):
        if frame.filename.startswith(_SKILL_FILE_PREFIX):
            skill = frame.filename[7:-1]
            lines = sources.get(skill, "").splitlines()
            code = lines[frame.lineno - 1].strip() if 0 < frame.lineno <= len(lines) else ""
            where = f" ({skill}.py line {frame.lineno}: {code})"
            break
    return f"{type(e).__name__}: {e}{where}"


def run_skill(source: str, env, params: dict, cfg, bus: EventBus, *, skill: str,
              version: int = 0, library: Optional[Dict[str, str]] = None,
              success: Optional[dict] = None) -> AttemptResult:
    library = dict(library or {})
    library[skill] = source  # the version under test shadows any saved copy
    guard = SafetyGuard(cfg)
    deadline = _Deadline(attempt_timeout_s(cfg))
    robot = _TimedRobot(env, guard, bus, cfg, deadline)
    guard.bind(robot)  # WS4.3: carry-path check before every motion, on the robot the skill uses
    _reset_perception_gate(env, bus)  # WS3.6: the confidence gate covers this attempt only
    scene_before = env.perception.observe()
    robot.last_scene = scene_before
    skills = SkillCaller(library, robot, scene_before, depth=1,
                         max_depth=cfg.safety.max_skill_depth, runner=_invoke)

    bus.emit("attempt.started", env=env.name, skill=skill, version=version)
    t0 = time.time()
    error = None
    deadline.begin()
    try:
        with _line_deadline(deadline):
            _invoke(source, skill, robot, scene_before, skills, params)
    except Exception as e:  # report every failure; nothing is swallowed
        error = _describe_error(e, library)
        _stop_arm(env, bus)
        if isinstance(e, AttemptTimeout):
            bus.emit("error", where="attempt.timeout", message=error)
    duration = time.time() - t0

    scene_after = env.perception.observe()
    bus.emit("scene.observed", scene=scene_after, image_path=scene_after.image_path)  # recordings replay it
    verify = evaluate(success, scene_after, cfg.verify.xy_tol, cfg.verify.z_tol) if success else None
    extra = getattr(env.arm, "safety_issues", lambda: [])()
    safety = audit_trace(robot.trace, cfg, extra, scene=scene_before)
    ok = error is None and safety.ok and (verify.ok if verify else True)
    if verify:
        bus.emit("verify.result", ok=verify.ok, detail=verify.detail)
    result = AttemptResult(ok=ok, env=env.name, skill=skill, version=version, trace=robot.trace,
                           error=error, verify=verify, safety=safety, scene_before=scene_before,
                           scene_after=scene_after, duration_s=round(duration, 3),
                           video_path=getattr(env.arm, "last_video_path", None))
    _analyse(result, cfg, bus, None if result.ok else _perception_gate(env, bus), robot=robot, env=env)
    bus.emit("attempt.finished", env=env.name, skill=skill, version=version, ok=ok,
             error=error, duration_s=result.duration_s, reason=result.reason())
    return result


def _attach_keyframes(result: AttemptResult, robot, env, bus: EventBus) -> None:
    """One annotated picture per phase, from the frames the robot captured after each motion call:
    the overhead view (objects boxed, gripper tip and the call's target drawn) beside the wrist view.
    This is what Droid sees of the attempt (report + attached images), so it can reason about a cup
    knocked over or squeezed out, not just read numbers. Never fails the attempt."""
    captures = getattr(robot, "captures", None)
    run_dir = getattr(bus, "run_dir", None)
    if not captures or run_dir is None:
        return
    try:
        from skillsmith.perception.annotate import annotate_keyframe, side_by_side, write_image

        camera = getattr(env.perception, "camera", None)
        scene = result.scene_after or result.scene_before
        calls = {c.index: c for c in result.trace}
        cls = result.classification.cls if result.classification else ("ok" if result.ok else "")
        for phase in result.phases:
            idx = max((i for i in captures if phase.start_index <= i <= phase.end_index), default=None)
            if idx is None:
                continue
            front, wrist, tip = captures[idx]
            call = calls.get(idx)
            target = None
            if call is not None and all(k in call.args for k in "xyz"):
                target = (call.args["x"], call.args["y"], call.args["z"])
            title = f"{phase.phase} {'ok' if phase.ok else 'FAIL'} · {cls} · after {call.name if call else '?'}"
            img = None
            if front is not None:
                img = annotate_keyframe(front, scene, camera,
                                        {"gripper": tuple(tip), "target": target, "title": title[:80]})
            if wrist is not None:
                img = wrist if img is None else side_by_side(img, wrist, ("overhead", "wrist"), height=360)
            if img is None:
                continue
            path = write_image(Path(run_dir) / "frames" / f"{result.env}_v{result.version}_{int(time.time())}_"
                                                          f"{phase.phase}.jpg", img)
            phase.frame_path = path
            result.keyframes[phase.phase] = path
    except Exception as e:  # pictures are evidence, never a reason to lose the attempt
        bus.emit("error", where="keyframes", message=f"{type(e).__name__}: {e}")


def _stop_arm(env, bus: EventBus) -> None:
    try:
        env.arm.stop()
    except Exception as stop_err:
        bus.emit("error", where="arm.stop", message=repr(stop_err))


def _reset_perception_gate(env, bus: EventBus) -> None:
    """Start the world model's gate window (`reset_gate()`), if the env has one (fake and sim do not)."""
    reset = getattr(env.perception, "reset_gate", None)
    if not callable(reset):
        return
    try:
        reset()
    except Exception as e:  # the attempt still runs; perception_ok() afterwards judges the scene
        bus.emit("error", where="perception.reset_gate", message=repr(e))


def _perception_gate(env, bus: EventBus) -> Optional[Tuple[bool, str]]:
    """`perception_ok()` from the live world model, if the env has one (fake and sim do not)."""
    check = getattr(env.perception, "perception_ok", None)
    if not callable(check):
        return None
    try:
        ok, reason = check()
    except NotImplementedError:
        return None
    except Exception as e:  # a gate that cannot answer is not a gate that passed
        bus.emit("error", where="perception_ok", message=repr(e))
        return False, f"perception_ok failed: {type(e).__name__}: {e}"
    return bool(ok), str(reason)


def _analyse(result: AttemptResult, cfg, bus: EventBus, perception: Optional[Tuple[bool, str]] = None,
             robot=None, env=None) -> None:
    """Phase checks + failure classification (WS4.4/WS4.5)."""
    from skillsmith.analysis import analyse_attempt

    try:
        extra = {} if perception is None else {"perception": perception}  # fake/sim: the plain contract
        result.phases, result.classification = analyse_attempt(result, cfg, **extra)
    except NotImplementedError:
        return
    except Exception as e:  # analysis must never lose the attempt; the report falls back to the raw error
        bus.emit("error", where="analysis", message=f"{type(e).__name__}: {e}")
        return
    if robot is not None and env is not None:
        _attach_keyframes(result, robot, env, bus)
    for phase in result.phases:
        bus.emit("phase.check", **dataclasses.asdict(phase))
    if result.classification is not None:
        bus.emit("attempt.classified", **dataclasses.asdict(result.classification))
