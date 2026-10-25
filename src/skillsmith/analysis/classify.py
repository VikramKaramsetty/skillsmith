"""Failure classifier: one class, bucket, phase, call, summary with numbers and a hint. WS4.5.

Plain rules over measured signals, no AI: the failing call and its exception type, grasp
results, the tip path, the scenes before/after, the success check's detail, safety issues and
the perception gate (`WorldModel.perception_ok()`, passed in by the runtime). Rules run in a
fixed order and the first that fires labels the attempt; anything no rule explains is UNKNOWN.

A wrong label is worse than UNKNOWN:
- the perception gate runs before every rule that reads the scene: a failed `perception_ok()`,
  a detection below `world.min_confidence`, an object appearing or vanishing, or a hidden object
  at the final check gives CAMERA_MOVED / CALIBRATION_DRIFT / PERCEPTION_UNCERTAIN, never a
  skill class;
- a skill label below MIN_SKILL_CONFIDENCE is reported as UNKNOWN, with the guess in evidence.

Rule order (first match wins):
  1 static checks rejected the skill                 STATIC_REJECTED
  2 a primitive call hit an I/O or robotd error      HARDWARE_FAULT
  3 perception gate                                  CAMERA_MOVED / CALIBRATION_DRIFT / PERCEPTION_UNCERTAIN
  4 wall-clock timeout (runtime AttemptTimeout)      TIMEOUT
  5 a Python error in the skill's own code           SKILL_ERROR
  6 WorldChanged, or a not-held object moved         KNOCKED_ON_APPROACH / COLLISION / BUMPED_BY_SOMETHING_ELSE
  7 the carry-path guard stopped a held object      COLLISION (phase carry, hint: travel height)
    ([CARRY_PATH] SafetyViolation or audit issue)
  8 CollisionError or a contact safety issue         COLLISION
  9 the safety guard refused a target                OBJECT_OUT_OF_REACH (the object itself) / UNSAFE_TARGET
 10 an object the skill or check named is not there  OBJECT_NOT_FOUND
 11 the gripper closed empty                         GRASP_MISS
 12 the tip reached an object and never grasped      NO_PICK
 13 the held object was left behind before release   SLIP_IN_CARRY
 14 released, then the success check failed          PLACE_OFF_TARGET / DROPPED_FROM_HEIGHT / PLACE_WRONG_HEIGHT
 15 anything else                                    UNKNOWN
Not produced here: AMBIGUOUS_TASK (task parser), HUMAN_REJECTED (repair loop).
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from skillsmith.analysis.phases import DROP_TOL, MOVE_EPS, grasped
from skillsmith.safety import CARRY_CODE
from skillsmith.types import (
    BUCKET_SKILL,
    FAILURE_CLASSES,
    AttemptResult,
    Classification,
    PhaseCheck,
    PrimitiveCall,
    Scene,
    SceneObject,
    Vec3,
)

PerceptionGate = Tuple[bool, str]  # WorldModel.perception_ok()

MIN_SKILL_CONFIDENCE = 0.6  # a skill label below this is reported as UNKNOWN
MIN_DETECTION = 0.5         # default for world.min_confidence
BUMP = 0.01                 # m; default for world.bump_threshold
RIM_FRACTION = 0.7          # gripped above this fraction of the object's height = near the rim
TOUCH = 0.015               # m; the tip (or held object) this close to an object touched it
FAR = 0.08                  # m; never closer than this to a moved object: something else moved it
AIM = 0.03                  # m; a refused target this close to an object's footprint was aimed at it
CARRY_TAG = f"[{CARRY_CODE}]"  # the carry-path guard's SafetyViolation (WS4.3)
CARRY_HINT = ("the carry: how high the held object travels over the objects it passes; its base must clear "
              "their tops (the guard's message gives the tip z needed), or the path must go around them")

HARDWARE_ERRORS = frozenset({
    "RobotdError", "FrameError", "OSError", "IOError", "ConnectionError", "ConnectionRefusedError",
    "ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError", "TimeoutError", "URLError",
    "HTTPError", "RemoteDisconnected", "SerialException", "SerialTimeoutException",
})
CODE_ERRORS = frozenset({
    "NameError", "UnboundLocalError", "TypeError", "AttributeError", "KeyError", "IndexError",
    "ValueError", "ZeroDivisionError", "ArithmeticError", "OverflowError", "RecursionError",
    "AssertionError", "LookupError", "StopIteration", "NotImplementedError", "RuntimeError",
})

_ERROR = re.compile(r"^([A-Za-z_][\w.]*): ?(.*)$", re.S)
_NOT_FOUND = re.compile(r"'(.+?)' not found in scene \(saw: (.*?)\)")
_CANNOT_SEE = re.compile(r"cannot see '(.+?)' \(saw: (.*?)\)")
_BUMPED = re.compile(r"^(.+?) moved")


# ---- what we know about the attempt --------------------------------------------------------

@dataclass
class _Facts:
    attempt: AttemptResult
    cfg: object
    phases: List[PhaseCheck]
    trace: List[PrimitiveCall]
    stop: Optional[int]  # trace position where execution stopped; None when it ran to the end
    kind: str            # exception type of the failure ("CollisionError"), "" when none
    message: str         # the error text after the type
    perception: Optional[PerceptionGate]

    @property
    def stop_call(self) -> Optional[PrimitiveCall]:
        return self.trace[self.stop] if self.stop is not None else None

    def index(self, pos: Optional[int]) -> Optional[int]:
        return self.trace[pos].index if pos is not None else None

    def phase(self, pos: Optional[int]) -> Optional[str]:
        """The phase of a trace position; the first failing phase when pos is None."""
        if not self.trace:
            return "approach"  # failed before the first call
        if pos is None:
            failing = next((p for p in self.phases if not p.ok), None)
            return failing.phase if failing else None
        index = self.trace[pos].index
        return next((p.phase for p in self.phases if p.start_index <= index <= p.end_index), None)

    def setting(self, section: str, key: str, default: float) -> float:
        part = self.cfg.get(section) if hasattr(self.cfg, "get") else None
        value = part.get(key) if part is not None else None
        return float(value) if value is not None else default


def _facts(attempt: AttemptResult, cfg, phases: List[PhaseCheck],
           perception: Optional[PerceptionGate]) -> _Facts:
    trace = list(attempt.trace)
    stop = next((i for i, c in enumerate(trace) if not c.ok), None)
    if stop is None and attempt.error and trace:
        stop = len(trace) - 1
    call_error = trace[stop].error if stop is not None and not trace[stop].ok else None
    kind, message = _split_error(call_error or attempt.error)
    return _Facts(attempt, cfg, phases, trace, stop, kind, message, perception)


def _split_error(text: Optional[str]) -> Tuple[str, str]:
    m = _ERROR.match(text or "")
    return (m.group(1).rsplit(".", 1)[-1], m.group(2)) if m else ("", text or "")


def _label(f: _Facts, cls: str, pos: Optional[int], summary: str, hint: str, confidence: float = 0.9,
           phase: Optional[str] = None, **evidence) -> Classification:
    return Classification(cls, FAILURE_CLASSES[cls], phase or f.phase(pos), f.index(pos), summary, hint,
                          round(confidence, 2), evidence=evidence)


# ---- 1-5: the error says it ----------------------------------------------------------------

def _static(f: _Facts) -> Optional[Classification]:
    if f.kind != "SkillValidationError":
        return None
    return _label(f, "STATIC_REJECTED", None, f.message, "the static check's message: use only the allowed API",
                  1.0, phase=None)


def _hardware(f: _Facts) -> Optional[Classification]:
    call = f.stop_call
    if call is None or call.ok or f.kind not in HARDWARE_ERRORS:
        return None
    return _label(f, "HARDWARE_FAULT", f.stop, f"{call.name} failed in the hardware layer: {f.kind}: {f.message}",
                  "robotd, the servos, cables and cameras; then retry", error=f.kind)


def _gate(f: _Facts) -> Optional[Classification]:
    """Perception we cannot trust: environment, never a skill label."""
    a = f.attempt
    if f.perception is not None and not f.perception[0]:
        reason = f.perception[1] or "perception check failed"
        cls, hint = _gate_class(reason)
        return _label(f, cls, f.stop, f"perception gate failed: {reason}", hint, perception=reason)
    floor = f.setting("world", "min_confidence", MIN_DETECTION)
    low = [o for s in (a.scene_before, a.scene_after) if s for o in s.objects if o.confidence < floor]
    if low:
        o = min(low, key=lambda o: o.confidence)
        return _label(f, "PERCEPTION_UNCERTAIN", f.stop,
                      f"{o.label} detected at confidence {o.confidence:.2f} (below {floor:.2f})",
                      "lighting and the camera view; then retry", object=o.label, detection=o.confidence)
    if a.scene_before is not None and a.scene_after is not None:
        before = {o.id: o.label for o in a.scene_before.objects}
        after = {o.id: o.label for o in a.scene_after.objects}
        if set(before) != set(after):
            gone = sorted(before[i] for i in set(before) - set(after))
            new = sorted(after[i] for i in set(after) - set(before))
            changes = "; ".join(s for s in (f"vanished: {', '.join(gone)}" if gone else "",
                                            f"appeared: {', '.join(new)}" if new else "") if s)
            return _label(f, "PERCEPTION_UNCERTAIN", f.stop,
                          f"object count changed during the attempt, {len(before)} before and {len(after)} "
                          f"after ({changes})", "the camera view: objects hidden, merged or misdetected",
                          vanished=gone, appeared=new)
    if not a.error and a.verify is not None and not a.verify.ok and a.scene_after is not None:
        hidden = [o.label for o in a.scene_after.objects if not o.visible]
        if hidden:
            return _label(f, "PERCEPTION_UNCERTAIN", None,
                          f"{', '.join(hidden)} hidden at the final check (last seen position used): "
                          f"{a.verify.detail}", "the camera view at the end: move the arm clear and re-check",
                          hidden=hidden)
    return None


def _gate_class(reason: str) -> Tuple[str, str]:
    text = reason.lower()
    if "marker" in text or ("camera" in text and "mov" in text):
        return "CAMERA_MOVED", "put the camera back or re-run camera calibration"
    if "calibrat" in text:
        return "CALIBRATION_DRIFT", "re-run calibration"
    return "PERCEPTION_UNCERTAIN", "lighting and the camera view; then retry"


def _timeout(f: _Facts) -> Optional[Classification]:
    if "Timeout" not in f.kind:
        return None
    return _label(f, "TIMEOUT", f.stop, f"stopped after {f.attempt.duration_s:.1f} s: {f.message}",
                  "the line that was running: loops and waits that never end")


def _code_error(f: _Facts) -> Optional[Classification]:
    if f.kind not in CODE_ERRORS:
        return None
    return _label(f, "SKILL_ERROR", f.stop, f.attempt.error or f"{f.kind}: {f.message}",
                  "the failing line of the skill", error=f.kind)


# ---- 6-10: motion and targets ----------------------------------------------------------------

def _bump(f: _Facts) -> Optional[Classification]:
    """A not-held object moved: did the arm do it (and when), or something else?"""
    a = f.attempt
    world_changed = f.kind == "WorldChanged"
    held_pos, held_obj, grip_tip = grasped(a)
    skip = _grasp_targets(f) | _supports(f) | ({held_obj.id} if held_obj else set())
    moved = _moved(a, f.setting("world", "bump_threshold", BUMP), skip)
    if world_changed and not moved:
        named = _BUMPED.match(f.message)
        obj = a.scene_before.find(named.group(1)) if named and a.scene_before else None
        moved = [(obj, 0.0)] if obj else []
    if not moved:
        if world_changed:
            return _label(f, "UNKNOWN", f.stop, f"the world model saw a bump ({f.message}) but no object "
                          "moved between the scenes", "the object tracks around the stop", 0.3)
        return None
    end = f.stop if world_changed else len(f.trace)
    moving = [i for i in range(end) if _moving(f.trace[i])]
    suspects = moving[-2:] if world_changed else moving  # a bump fires a frame or two after its cause
    holding = _holding_positions(f, held_pos)
    offset = (grip_tip[2] - held_obj.z) if held_obj and grip_tip else 0.0
    obj, dist = moved[0]

    def gap(i: int) -> float:
        return _clearance(f.trace[i], obj, held_obj if i in holding else None, offset)

    touched = min(((gap(i), i) for i in suspects), default=None)
    if touched is not None and touched[0] < TOUCH:
        pos = touched[1]
        who = f"held {held_obj.label}" if pos in holding and held_obj else "the tip"
        summary = (f"{obj.label} moved {_cm(dist)} when {who} passed {_cm(touched[0])} from it during "
                   f"{f.trace[pos].name} to {_fmt(f.trace[pos].ee_after)}")
        if held_pos is None or pos < held_pos:
            return _label(f, "KNOCKED_ON_APPROACH", pos, summary + ", before the grasp",
                          f"the approach phase: the tip path past {obj.label} before the grasp",
                          object=obj.label, moved=dist)
        return _label(f, "COLLISION", pos, summary, _collision_hint(f.phase(pos)), object=obj.label, moved=dist)
    closest = min((gap(i) for i in moving), default=math.inf)
    if closest > FAR:
        near = "did not move" if math.isinf(closest) else f"stayed at least {_cm(closest)} away"
        return _label(f, "BUMPED_BY_SOMETHING_ELSE", f.stop,
                      f"{obj.label} moved {_cm(dist)} while the arm {near}: something else moved it",
                      "the table: what moved the object; reset it and retry", object=obj.label, moved=dist)
    if world_changed:
        return _label(f, "UNKNOWN", f.stop, f"{obj.label} moved ({f.message}); the arm came within "
                      f"{_cm(closest)} but did not clearly touch it", "the object track and the tip path", 0.4)
    return None


def _moved(a: AttemptResult, threshold: float, skip: set) -> List[Tuple[SceneObject, float]]:
    """(object as it was before, how far it moved) for objects not in `skip`, farthest first."""
    if a.scene_before is None or a.scene_after is None:
        return []
    after = {o.id: o for o in a.scene_after.objects}
    out = []
    for o in a.scene_before.objects:
        b = after.get(o.id)
        if b is None or o.id in skip:
            continue
        d = math.dist((o.x, o.y, o.z), (b.x, b.y, b.z))
        if d > threshold:
            out.append((o, d))
    return sorted(out, key=lambda t: -t[1])


def _grasp_targets(f: _Facts) -> set:
    """Objects the gripper closed on (or tried to): moving them is the grasp's business, not a bump."""
    out = set()
    for c in f.trace:
        near = _nearest(f.attempt.scene_before, c.ee_after) if c.name == "grasp" else None
        if near and near[1] < max(near[0].width / 2, 0.02):
            out.add(near[0].id)
    return out


def _supports(f: _Facts) -> set:
    """Objects under a release point: something was set down on them, so a nudge is expected."""
    out, holding = set(), False
    for c in f.trace:
        if c.name == "grasp":
            holding = c.ok and c.result is True
        elif c.name == "release" and holding:
            holding = False
            near = _nearest(f.attempt.scene_before, c.ee_before)
            if near and near[1] < near[0].width / 2:
                out.add(near[0].id)
    return out


def _holding_positions(f: _Facts, held_pos: Optional[int]) -> set:
    if held_pos is None:
        return set()
    release = next((i for i in range(held_pos + 1, len(f.trace)) if f.trace[i].name == "release"), len(f.trace))
    return set(range(held_pos + 1, release))


def _clearance(call: PrimitiveCall, obj: SceneObject, held: Optional[SceneObject], offset: float) -> float:
    """Closest gap between what the call moved (the tip, or the held object) and an object's volume."""
    reach = held.width / 2 if held else 0.0
    best = math.inf
    for p in _samples(call.ee_before, call.ee_after):
        side = max(0.0, math.hypot(p[0] - obj.x, p[1] - obj.y) - obj.width / 2 - reach)
        up = max(0.0, p[2] - offset - obj.top)  # from the lowest point: the tip, or the held object's base
        best = min(best, math.hypot(side, up))
    return best


def _samples(a: Optional[Vec3], b: Optional[Vec3]) -> List[Vec3]:
    if a is None or b is None:
        return [p for p in (a, b) if p is not None]
    n = max(1, min(100, math.ceil(math.dist(a, b) / 0.005)))
    return [tuple(a[k] + (b[k] - a[k]) * t / n for k in range(3)) for t in range(n + 1)]


def _moving(call: PrimitiveCall) -> bool:
    return (call.ee_before is not None and call.ee_after is not None
            and math.dist(call.ee_before, call.ee_after) >= MOVE_EPS)


def _carry_path(f: _Facts) -> Optional[Classification]:
    """The carry-path guard (WS4.3) stopped a held object before it hit something, or the audit found
    a carry segment too close to an object: a collision in the carry, fixed by the travel height."""
    if f.kind == "SafetyViolation" and CARRY_TAG in f.message:
        message = f.message.replace(CARRY_TAG, "").strip()
        return _label(f, "COLLISION", f.stop, f"the carry-path guard stopped the move: {message}", CARRY_HINT,
                      phase="carry", stopped_by="carry-path guard")
    issue = next((i for i in f.attempt.safety.issues if i.code == CARRY_CODE), None)
    if issue is None:
        return None
    pos = next((p for p, c in enumerate(f.trace) if c.index == issue.call_index), f.stop)
    return _label(f, "COLLISION", pos, issue.message.replace(CARRY_TAG, "").strip(), CARRY_HINT, phase="carry")


def _collision(f: _Facts) -> Optional[Classification]:
    if f.kind == "CollisionError":
        target = _target(f.stop_call)
        where = f" (moving to {_fmt(target)})" if target else ""
        return _label(f, "COLLISION", f.stop, f"{f.message}{where}", _collision_hint(f.phase(f.stop)))
    issue = next((i for i in f.attempt.safety.issues if i.code == "COLLISION"), None)
    if issue is None:
        return None
    pos = next((p for p, c in enumerate(f.trace) if c.index == issue.call_index), f.stop)
    return _label(f, "COLLISION", pos, issue.message, _collision_hint(f.phase(pos)))


def _collision_hint(phase: Optional[str]) -> str:
    return {
        "approach": "the approach phase: the tip path against the objects before the grasp",
        "grasp": "the approach phase: the tip path against the objects before the grasp",
        "lift": "the carry: the held object's base against the tops of the objects it passes",
        "carry": "the carry: the held object's base against the tops of the objects it passes",
        "place": "the place phase: the held object's base against its support's top",
    }.get(phase or "", "the trace: the tip path against the other objects and the table")


def _refused(f: _Facts) -> Optional[Classification]:
    if f.kind not in ("SafetyViolation", "Unreachable"):
        return None
    call = f.stop_call
    if "[CALL_BUDGET]" in f.message:
        return _label(f, "SKILL_ERROR", f.stop, f.message, "loops that never end")
    target = _target(call)
    if target is None:
        return _label(f, "UNSAFE_TARGET", f.stop, f"the safety guard refused it: {f.message}",
                      "the call's arguments against the safety limits")
    obj = _aimed_at(f, target)
    if obj is not None and not _in_workspace(f.cfg, obj.x, obj.y):
        r, ws = math.hypot(obj.x, obj.y), f.cfg.workspace
        return _label(f, "OBJECT_OUT_OF_REACH", f.stop,
                      f"{obj.label} at ({obj.x:.3f}, {obj.y:.3f}) is {_cm(r)} from the base (reach "
                      f"{_cm(ws.min_reach)} to {_cm(ws.max_reach)}); {call.name} to {_fmt(target)} was refused",
                      f"move the {obj.label} inside the workspace", object=obj.label, radius=round(r, 4))
    aimed = f" (aimed at {obj.label}, which is reachable)" if obj is not None else ""
    return _label(f, "UNSAFE_TARGET", f.stop, f"{call.name} to {_fmt(target)} refused{aimed}: {f.message}",
                  "the refused call: its target against the workspace limits", 0.7 if f.kind == "Unreachable" else 0.9)


def _target(call: Optional[PrimitiveCall]) -> Optional[Vec3]:
    if call is None or call.name != "move_to" or not all(k in call.args for k in "xyz"):
        return None
    return (float(call.args["x"]), float(call.args["y"]), float(call.args["z"]))


def _aimed_at(f: _Facts, target: Vec3) -> Optional[SceneObject]:
    near = _nearest(_latest_scene(f), target)
    return near[0] if near and near[1] < near[0].width / 2 + AIM else None


def _in_workspace(cfg, x: float, y: float) -> bool:
    ws = cfg.workspace
    r = math.hypot(x, y)
    return ws.min_reach <= r <= ws.max_reach and ws.x[0] <= x <= ws.x[1] and ws.y[0] <= y <= ws.y[1]


def _latest_scene(f: _Facts) -> Optional[Scene]:
    end = f.stop if f.stop is not None else len(f.trace)
    for c in reversed(f.trace[:end]):
        if c.name == "look" and isinstance(c.result, Scene):
            return c.result
    return f.attempt.scene_before


def _not_found(f: _Facts) -> Optional[Classification]:
    a = f.attempt
    m = _NOT_FOUND.search(a.error) if a.error else None
    if m is None and not a.error and a.verify is not None and not a.verify.ok:
        m = _CANNOT_SEE.search(a.verify.detail)
    if m is None:
        return None
    query, saw = m.group(1), m.group(2)
    return _label(f, "OBJECT_NOT_FOUND", f.stop, f"no '{query}' on the table (saw: {saw})",
                  f"put the {query} on the table in view of the camera, or check the task's object names",
                  0.8, query=query)


# ---- 11-14: what the gripper and the objects did --------------------------------------------

def _grasp_miss(f: _Facts) -> Optional[Classification]:
    if not any(p.phase == "grasp" for p in f.phases) or grasped(f.attempt)[0] is not None:
        return None
    pos = max(i for i, c in enumerate(f.trace) if c.name == "grasp")
    tip = f.trace[pos].ee_after or f.trace[pos].ee_before
    near = _nearest(f.attempt.scene_before, tip)
    if tip is None or near is None:
        return None
    obj, off = near
    where, why = "", ""
    if tip[2] > obj.top:
        where, why = f"{_cm(tip[2] - obj.top)} above its top", "above the object's top"
    elif tip[2] < obj.z:
        where, why = f"{_cm(obj.z - tip[2])} below its base", "below the object's base"
    else:
        where = f"at {(tip[2] - obj.z) / obj.height:.0%} of its height" if obj.height > 0 else "beside it"
    if off > obj.width / 2 and not why:
        why = "beside the object"
    hint = (f"the grasp phase: the tip was {why} when the gripper closed" if why
            else "the grasp phase: where the tip was, against the object, when the gripper closed")
    summary = (f"gripper closed empty at {_fmt(tip)}: {_cm(off)} off {obj.label} centre "
               f"(radius {_cm(obj.width / 2)}), {where}")
    return _label(f, "GRASP_MISS", pos, summary, hint, 0.9 if why else 0.7,
                  phase="grasp", object=obj.label, xy_off=round(off, 4), tip_z=tip[2])


def _no_pick(f: _Facts) -> Optional[Classification]:
    if any(c.name == "grasp" for c in f.trace):
        return None
    for i, c in enumerate(f.trace):
        obj = _inside(f.attempt.scene_before, c.ee_after)
        if obj is None:
            continue
        leave = next((j for j in range(i + 1, len(f.trace)) if _moving(f.trace[j])), i)
        return _label(f, "NO_PICK", leave,
                      f"the tip reached {obj.label} at z={c.ee_after[2]:.3f} (call #{c.index}) and left without "
                      "closing the gripper; robot.grasp() was never called",
                      "the trace: the gripper never closed between reaching the object and leaving it", 0.85,
                      phase="grasp", object=obj.label)
    v = f.attempt.verify
    if f.attempt.error or v is None or v.ok or not f.trace:
        return None
    return _label(f, "NO_PICK", len(f.trace) - 1, f"the skill ended without ever closing the gripper: {v.detail}",
                  "the trace: whether the object was ever held", 0.5, phase="grasp")


def _inside(scene: Optional[Scene], p: Optional[Vec3]) -> Optional[SceneObject]:
    if scene is None or p is None:
        return None
    return next((o for o in scene.objects
                 if math.hypot(p[0] - o.x, p[1] - o.y) < o.width / 2 and o.z <= p[2] <= o.top), None)


def _slip(f: _Facts) -> Optional[Classification]:
    held_pos, obj, grip = grasped(f.attempt)
    after = _after(f.attempt, obj)
    if held_pos is None or obj is None or grip is None or after is None:
        return None
    release = next((i for i in range(held_pos + 1, len(f.trace)) if f.trace[i].name == "release"), None)
    end = f.trace[release].ee_before if release is not None else f.trace[-1].ee_after
    if end is None:
        return None
    near = max(obj.width / 2, 0.02)
    gap = math.hypot(end[0] - after.x, end[1] - after.y)
    fell = release is None and (end[2] - (grip[2] - obj.z)) - after.z > DROP_TOL
    if gap <= near and not fell:
        return None
    holding = range(held_pos + 1, release if release is not None else len(f.trace))
    leave = next((i for i in holding if _left_behind(f.trace[i], after, near)), None)
    confidence = 0.9
    if leave is None:
        if release is not None:
            return None  # it stayed with the gripper until the release: the place rules decide
        leave, confidence = (f.stop if f.stop is not None else len(f.trace) - 1), 0.7
    frac = (grip[2] - obj.z) / obj.height if obj.height > 0 else 0.0
    hint = (f"the grasp phase: gripped at {frac:.0%} of the object's height, near its top; then the carry"
            if frac > RIM_FRACTION else "the object track: when the held object left the gripper")
    return _label(f, "SLIP_IN_CARRY", leave,
                  f"{obj.label} gripped at z={grip[2]:.3f} ({frac:.0%} of its height) was held, then lost during "
                  f"a {_move(f.trace[leave])} move; it ended {_cm(gap)} from the gripper", hint, confidence,
                  object=obj.label, grip_fraction=round(frac, 3), gap=round(gap, 4))


def _left_behind(call: PrimitiveCall, after: SceneObject, near: float) -> bool:
    """The call's path passed where the object ended up, and finished away from it."""
    if not _moving(call):
        return False
    passed = min(math.hypot(p[0] - after.x, p[1] - after.y) for p in _samples(call.ee_before, call.ee_after))
    return passed <= near < math.hypot(call.ee_after[0] - after.x, call.ee_after[1] - after.y)


def _move(call: PrimitiveCall) -> str:
    if not _moving(call):
        return "0.0 cm"
    a, b = call.ee_before, call.ee_after
    side, vertical = math.hypot(b[0] - a[0], b[1] - a[1]), abs(b[2] - a[2])
    return f"{_cm(side)} sideways" if side >= vertical else f"{_cm(vertical)} vertical"


def _place(f: _Facts) -> Optional[Classification]:
    a = f.attempt
    held_pos, obj, grip = grasped(a)
    after = _after(a, obj)
    if held_pos is None or obj is None or grip is None or after is None or a.verify is None or a.verify.ok:
        return None
    release = next((i for i in range(held_pos + 1, len(f.trace)) if f.trace[i].name == "release"), None)
    tip = f.trace[release].ee_before if release is not None else None
    if tip is None:
        return None
    detail = a.verify.detail
    settled = math.hypot(tip[0] - after.x, tip[1] - after.y)
    fall = tip[2] - (grip[2] - obj.z) - after.z  # base height at release minus where it landed
    numbers = dict(object=obj.label, release=tip, settled=round(settled, 4), fall=round(fall, 4))
    released = f"released {obj.label} at {_fmt(tip)}"
    # where the skill chose the spot: the last move before the release (the place descent)
    put = next((i for i in range(release - 1, held_pos, -1) if _moving(f.trace[i])), release)
    if " cm from " in detail:
        if settled <= f.setting("verify", "xy_tol", 0.03):
            return _label(f, "PLACE_OFF_TARGET", put, f"{released}, where it stayed: {detail}",
                          "expected vs actual: where the object was let go against where the check wanted it", **numbers)
        return _drop(f, release, obj, fall, detail, numbers) if fall > DROP_TOL else None
    if "below" in detail or "resting on top" in detail:
        if fall > DROP_TOL:
            return _drop(f, release, obj, fall, detail, numbers)
        return _label(f, "PLACE_WRONG_HEIGHT", put, f"{released}: {detail}",
                      "expected vs actual: the object's final height against the check", **numbers)
    return None


def _drop(f: _Facts, pos: int, obj: SceneObject, fall: float, detail: str, numbers: dict) -> Classification:
    return _label(f, "DROPPED_FROM_HEIGHT", pos, f"let go of {obj.label} {_cm(fall)} above where it landed: {detail}",
                  "the release phase: how far above its support the object was let go", **numbers)


def _after(a: AttemptResult, obj: Optional[SceneObject]) -> Optional[SceneObject]:
    if obj is None or a.scene_after is None:
        return None
    return next((o for o in a.scene_after.objects if o.id == obj.id), None)


# ---- entry point --------------------------------------------------------------------------

RULES: Sequence[Callable[[_Facts], Optional[Classification]]] = (
    _static, _hardware, _gate, _timeout, _code_error, _bump, _carry_path, _collision, _refused, _not_found,
    _grasp_miss, _no_pick, _slip, _place,
)


def classify(attempt: AttemptResult, cfg, phases: List[PhaseCheck],
             perception: Optional[PerceptionGate] = None) -> Optional[Classification]:
    """The failure's label; None when the attempt succeeded."""
    if attempt.ok:
        return None
    f = _facts(attempt, cfg, phases, perception)
    label = next((c for c in (rule(f) for rule in RULES) if c is not None), None)
    return _gated(label or _unknown(f))


def _unknown(f: _Facts) -> Classification:
    reason = dataclasses.replace(f.attempt, classification=None).reason()
    return _label(f, "UNKNOWN", f.stop, reason, "all the evidence: no rule explained it", 0.3)


def _gated(c: Classification) -> Classification:
    """A skill label we are not sure of goes out as UNKNOWN, with the guess kept as evidence."""
    if c.bucket != BUCKET_SKILL or c.confidence >= MIN_SKILL_CONFIDENCE:
        return c
    return Classification("UNKNOWN", FAILURE_CLASSES["UNKNOWN"], c.phase, c.call_index,
                          f"{c.summary} (best guess {c.cls}, confidence {c.confidence:.2f})", c.hint,
                          c.confidence, evidence={**c.evidence, "guess": c.cls})


# ---- numbers --------------------------------------------------------------------------------

def _nearest(scene: Optional[Scene], p: Optional[Vec3]) -> Optional[Tuple[SceneObject, float]]:
    if scene is None or p is None or not scene.objects:
        return None
    obj = min(scene.objects, key=lambda o: math.hypot(p[0] - o.x, p[1] - o.y))
    return obj, math.hypot(p[0] - obj.x, p[1] - obj.y)


def _cm(metres: float) -> str:
    return f"{metres * 100:.1f} cm"


def _fmt(p: Optional[Sequence[float]]) -> str:
    return f"({p[0]:.3f}, {p[1]:.3f}, {p[2]:.3f})" if p else "?"
