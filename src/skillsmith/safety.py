"""Cartesian safety envelope. See docs/SAFETY.md.

SafetyGuard runs BEFORE every motion (raises SafetyViolation, nothing moves).
audit_trace re-checks a finished trace (used for the sim gate report).
Joint limits and per-step clamps live lower down, in robotd (last line of defence).

Carry-path check (WS4.3): while the gripper holds an object, every commanded move segment is
checked against the scene's known objects (positions + sizes from the known-object table), so a
held object never sweeps through one. Bodies are vertical cylinders (width = diameter); the
clearance is the signed distance between the held body and each object along the straight
segment, and it must stay >= `safety.carry_clearance` (default 1 cm). Two kinds of move are
exempt: moving away from what the held object touches (lifting off its support: clearance only
grows), and a mostly vertical descent (the place phase: lowering onto or into the target). Place
height and placement next to a neighbour are the sim gate's and the camera's job.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Optional, Tuple

from skillsmith.types import PrimitiveCall, SafetyIssue, SafetyReport, SafetyViolation, Scene, SceneObject, Vec3

DEFAULT_CARRY_CLEARANCE = 0.01  # m; until config/robot.yaml has safety.carry_clearance
CARRY_CODE = "CARRY_PATH"
_GRASP_SLACK = 0.02  # m; how far outside an object's volume the tip may be and still be holding it


class SafetyGuard:
    def __init__(self, cfg):
        self.ws = cfg.workspace
        self.s = cfg.safety
        self.calls = 0
        self.carry = CarryTracker(carry_clearance(cfg))
        self._robot = None

    def bind(self, robot) -> None:
        """Let check_target see the robot's tip, grip and latest scene (public Robot API only),
        so it also runs the carry-path check before every motion."""
        self._robot = robot

    def target_issues(self, p: Vec3, call_index: Optional[int] = None) -> List[SafetyIssue]:
        x, y, z = p
        issues = []
        for axis, v, (lo, hi) in (("x", x, self.ws.x), ("y", y, self.ws.y), ("z", z, self.ws.z)):
            if not lo <= v <= hi:
                issues.append(SafetyIssue(
                    "OUT_OF_BOX", f"{axis}={v:.3f} outside workspace [{lo}, {hi}]", call_index))
        if z < self.s.min_tip_z:
            issues.append(SafetyIssue(
                "TOO_LOW", f"z={z:.3f} below min tip height {self.s.min_tip_z}", call_index))
        r = math.hypot(x, y)
        if r > self.ws.max_reach:
            issues.append(SafetyIssue(
                "OUT_OF_REACH", f"radius {r:.3f} > max reach {self.ws.max_reach}", call_index))
        if r < self.ws.min_reach:
            issues.append(SafetyIssue(
                "TOO_CLOSE", f"radius {r:.3f} < min reach {self.ws.min_reach}", call_index))
        return issues

    def check_target(self, p: Vec3, call_index: Optional[int] = None) -> None:
        issues = self.target_issues(p, call_index)
        if issues:
            raise SafetyViolation("; ".join(f"[{i.code}] {i.message}" for i in issues))
        robot = self._robot
        if robot is not None:
            self.check_carry(robot.position(), p, call_index, robot.holding(), robot.last_scene)

    def check_carry(self, start: Vec3, end: Vec3, call_index: Optional[int], holding: bool,
                    scene: Optional[Scene]) -> None:
        """Raise before motion if the held object would pass too close to (or through) a known object."""
        self.carry.update(start, holding, scene)
        issues = self.carry.segment_issues(start, end, call_index)
        if issues:
            raise SafetyViolation("; ".join(f"[{i.code}] {i.message}" for i in issues))

    def clamp_speed(self, speed: Optional[float]) -> float:
        if speed is None:
            return float(self.s.default_tcp_speed)
        if speed <= 0:
            raise SafetyViolation(f"speed must be positive, got {speed}")
        return float(min(speed, self.s.max_tcp_speed))

    def count_call(self) -> int:
        self.calls += 1
        if self.calls > self.s.max_calls:
            raise SafetyViolation(
                f"[CALL_BUDGET] more than {self.s.max_calls} primitive calls in one attempt")
        return self.calls - 1


_ERROR_CODES = (
    ("Unreachable", "UNREACHABLE"),
    ("CollisionError", "COLLISION"),
    ("SafetyViolation", "ENVELOPE"),
)


def audit_trace(trace: Iterable[PrimitiveCall], cfg,
                extra: Iterable[SafetyIssue] = (), scene: Optional[Scene] = None) -> SafetyReport:
    """Re-check every recorded call. `extra` carries backend findings (e.g. sim contacts).
    `scene` (the scene before the attempt) enables the carry-path re-check from the first call;
    without it the re-check starts at the first `look` in the trace."""
    trace = list(trace)
    guard = SafetyGuard(cfg)
    issues: List[SafetyIssue] = list(extra)
    n = 0
    for call in trace:
        n += 1
        if call.name == "move_to":
            a = call.args
            issues += guard.target_issues((a["x"], a["y"], a["z"]), call.index)
        if call.error:
            if f"[{CARRY_CODE}]" in call.error:
                issues.append(SafetyIssue(CARRY_CODE, call.error, call.index))
                continue
            for needle, code in _ERROR_CODES:
                if call.error.startswith(needle):
                    issues.append(SafetyIssue(code, call.error, call.index))
                    break
    issues += carry_trace_issues(trace, cfg, scene)
    if n > cfg.safety.max_calls:
        issues.append(SafetyIssue("CALL_BUDGET", f"{n} calls > {cfg.safety.max_calls}"))
    return SafetyReport(ok=not issues, issues=issues)


# ---- carry-path check (WS4.3) -----------------------------------------------------------------
def carry_clearance(cfg) -> float:
    """`safety.carry_clearance` in metres (DEFAULT_CARRY_CLEARANCE until the key exists)."""
    return float(getattr(cfg.safety, "carry_clearance", DEFAULT_CARRY_CLEARANCE))


@dataclass(frozen=True)
class HeldBody:
    """What the gripper carries: a vertical cylinder whose base sits `offset` m from the tip."""
    id: Optional[str]
    label: str
    radius: float
    height: float
    offset: float  # held base z minus tip z (negative: the base hangs below the tip)

    def span(self, tip_z: float) -> Tuple[float, float]:
        base = tip_z + self.offset
        return base, base + self.height


def held_body(scene: Optional[Scene], tip: Vec3) -> HeldBody:
    """The scene object the tip is gripping, or a bare point. Of the objects whose volume (plus
    slack) holds the tip, prefer the one whose height span contains it, then the nearest in xy."""
    best: Optional[SceneObject] = None
    best_key = (math.inf, math.inf)
    for o in scene.objects if scene else ():
        d = math.hypot(o.x - tip[0], o.y - tip[1])
        outside_z = max(o.z - tip[2], tip[2] - o.top, 0.0)
        key = (round(outside_z, 4), d)
        if d <= o.width / 2 + _GRASP_SLACK and outside_z <= _GRASP_SLACK and key < best_key:
            best, best_key = o, key
    if best is None:
        return HeldBody(None, "the held object", 0.0, 0.0, 0.0)
    return HeldBody(best.id, best.label, best.width / 2, best.height, best.z - tip[2])


def clearance(tip: Vec3, held: HeldBody, obj: SceneObject) -> float:
    """Signed distance (m) between the held cylinder at `tip` and `obj`; negative = overlapping."""
    gap_xy = math.hypot(tip[0] - obj.x, tip[1] - obj.y) - held.radius - obj.width / 2
    base, top = held.span(tip[2])
    gap_z = max(obj.z - top, base - obj.top)
    if gap_xy > 0 and gap_z > 0:
        return math.hypot(gap_xy, gap_z)
    return max(gap_xy, gap_z)


def _lerp(a: Vec3, b: Vec3, t: float) -> Vec3:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def min_clearance(start: Vec3, end: Vec3, held: HeldBody, obj: SceneObject) -> Tuple[float, float]:
    """(smallest clearance, where along the segment 0..1). The signed distance between two convex
    bodies is convex in a linear motion, so a ternary search finds the minimum."""
    lo, hi = 0.0, 1.0
    for _ in range(60):
        m1, m2 = lo + (hi - lo) / 3, hi - (hi - lo) / 3
        if clearance(_lerp(start, end, m1), held, obj) <= clearance(_lerp(start, end, m2), held, obj):
            hi = m2
        else:
            lo = m1
    t = (lo + hi) / 2
    return min((clearance(_lerp(start, end, k), held, obj), k) for k in (0.0, t, 1.0))


def is_place_descent(start: Vec3, end: Vec3) -> bool:
    """A mostly vertical descent (the place phase): lowering onto or into the target, not carrying."""
    dz = end[2] - start[2]
    return dz < 0 and math.hypot(end[0] - start[0], end[1] - start[1]) <= -dz


def _fmt(p: Vec3) -> str:
    return f"({p[0]:.3f}, {p[1]:.3f}, {p[2]:.3f})"


class CarryTracker:
    """Follows grasp/release and checks move segments while holding. One per attempt."""

    def __init__(self, margin: float):
        self.margin = margin
        self.held: Optional[HeldBody] = None
        self.scene: Optional[Scene] = None
        self._moved: Dict[str, SceneObject] = {}  # objects put down since `scene` was observed

    def observe(self, scene: Optional[Scene]) -> None:
        if scene is not None and scene is not self.scene:
            self.scene, self._moved = scene, {}

    def grasped(self, tip: Vec3) -> None:
        self.held = held_body(self.scene, tip)

    def released(self, tip: Vec3) -> None:
        held, self.held = self.held, None
        obj = self._find(held.id) if held and held.id else None
        if obj is not None:
            self._moved[obj.id] = replace(obj, x=tip[0], y=tip[1], z=max(0.0, tip[2] + held.offset))

    def update(self, tip: Vec3, holding: bool, scene: Optional[Scene]) -> None:
        self.observe(scene)
        if holding and self.held is None:
            self.grasped(tip)
        elif not holding and self.held is not None:
            self.released(tip)

    def obstacles(self) -> List[SceneObject]:
        objs = [self._moved.get(o.id, o) for o in self.scene.objects] if self.scene else []
        held_id = self.held.id if self.held else None
        return [o for o in objs if o.id != held_id]

    def segment_issues(self, start: Vec3, end: Vec3, call_index: Optional[int]) -> List[SafetyIssue]:
        if self.held is None or is_place_descent(start, end):
            return []
        issues = []
        for obj in self.obstacles():
            c, _ = min_clearance(start, end, self.held, obj)
            if c >= self.margin:
                continue
            if c >= clearance(start, self.held, obj) - 1e-4:
                continue  # moving away from what it already touches (lifting off its support)
            issues.append(SafetyIssue(CARRY_CODE, self._message(start, end, call_index, obj, c), call_index))
        return issues

    def _message(self, start: Vec3, end: Vec3, call_index: Optional[int], obj: SceneObject, c: float) -> str:
        held = self.held
        call = f" (call {call_index})" if call_index is not None else ""
        how = "sweeps through" if c < 0 else "passes too close to"
        need_z = obj.top - held.offset + self.margin
        return (f"carrying {held.label}, move {_fmt(start)} -> {_fmt(end)}{call} {how} {obj.label}: "
                f"clearance {c * 100:.1f} cm < {self.margin * 100:.1f} cm required. Raise the travel "
                f"height so the tip is at z >= {need_z:.3f} over {obj.label} (its top is z={obj.top:.3f}, "
                f"the held base hangs {-held.offset * 100:.1f} cm below the tip) or route around it")

    def _find(self, obj_id: str) -> Optional[SceneObject]:
        if obj_id in self._moved:
            return self._moved[obj_id]
        return next((o for o in self.scene.objects if o.id == obj_id), None) if self.scene else None


def _segment(call: PrimitiveCall, cfg) -> Optional[Tuple[Vec3, Vec3]]:
    """The commanded straight segment of a motion call, from the recorded tip positions."""
    start = call.ee_before
    if start is None:
        return None
    if call.name == "move_to":
        return start, (call.args["x"], call.args["y"], call.args["z"])
    if call.name == "home":
        return start, tuple(cfg.robot.home_xyz)
    if call.name in ("align_over", "approach_side") and call.ee_after is not None:
        return start, call.ee_after
    return None


def carry_trace_issues(trace: Iterable[PrimitiveCall], cfg, scene: Optional[Scene] = None) -> List[SafetyIssue]:
    """Post-hoc carry-path check: replay grasp/release from the trace against the latest scene."""
    tracker = CarryTracker(carry_clearance(cfg))
    tracker.observe(scene)
    issues: List[SafetyIssue] = []
    for call in trace:
        if call.name == "look" and isinstance(call.result, Scene):
            tracker.observe(call.result)
        elif call.name == "grasp" and call.result is True and call.ee_before is not None:
            tracker.grasped(call.ee_before)
        elif call.name == "release" and call.ee_before is not None:
            tracker.released(call.ee_before)
        elif call.error and f"[{CARRY_CODE}]" in call.error:
            continue  # the guard already stopped this move; audit_trace reports its error
        else:
            seg = _segment(call, cfg)
            if seg is not None:
                issues += tracker.segment_issues(seg[0], seg[1], call.index)
    return issues
