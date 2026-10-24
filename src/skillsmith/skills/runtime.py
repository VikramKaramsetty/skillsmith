"""Run a skill: validate, exec with stripped builtins, record the trace, verify, audit.

`run_skill` is the single entry point used by the CLI, the repair loop and tests.
"""

from __future__ import annotations

import builtins
import math
import time
import traceback
from typing import Any, Dict, Optional

from skillsmith.events import EventBus
from skillsmith.primitives import Robot, SkillCaller
from skillsmith.safety import SafetyGuard, audit_trace
from skillsmith.skills.validator import validate
from skillsmith.types import AttemptResult, Scene, SkillError
from skillsmith.verify import evaluate

SAFE_BUILTINS: Dict[str, Any] = {
    name: getattr(builtins, name)
    for name in ("abs", "min", "max", "round", "len", "range", "float", "int", "str", "bool",
                 "list", "dict", "tuple", "enumerate", "zip", "sorted", "sum", "any", "all",
                 "ValueError", "RuntimeError")
}
SAFE_BUILTINS["SkillError"] = SkillError


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
        if frame.filename.startswith("<skill:"):
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
    robot = Robot(env, guard, bus, cfg)
    scene_before = env.perception.observe()
    robot.last_scene = scene_before
    skills = SkillCaller(library, robot, scene_before, depth=1,
                         max_depth=cfg.safety.max_skill_depth, runner=_invoke)

    bus.emit("attempt.started", env=env.name, skill=skill, version=version)
    t0 = time.time()
    error = None
    try:
        _invoke(source, skill, robot, scene_before, skills, params)
    except Exception as e:  # report every failure; nothing is swallowed
        error = _describe_error(e, library)
        try:
            env.arm.stop()
        except Exception as stop_err:
            bus.emit("error", where="arm.stop", message=repr(stop_err))
    duration = time.time() - t0

    scene_after = env.perception.observe()
    verify = evaluate(success, scene_after, cfg.verify.xy_tol, cfg.verify.z_tol) if success else None
    extra = getattr(env.arm, "safety_issues", lambda: [])()
    safety = audit_trace(robot.trace, cfg, extra)
    ok = error is None and safety.ok and (verify.ok if verify else True)
    if verify:
        bus.emit("verify.result", ok=verify.ok, detail=verify.detail)
    result = AttemptResult(ok=ok, env=env.name, skill=skill, version=version, trace=robot.trace,
                           error=error, verify=verify, safety=safety, scene_before=scene_before,
                           scene_after=scene_after, duration_s=round(duration, 3),
                           video_path=getattr(env.arm, "last_video_path", None))
    bus.emit("attempt.finished", env=env.name, skill=skill, version=version, ok=ok,
             error=error, duration_s=result.duration_s, reason=result.reason())
    return result
