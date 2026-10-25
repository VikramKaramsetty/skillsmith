"""The skill-facing API. A skill can touch ONLY `Robot` (as `robot`), a `Scene` (as
`scene`) and `SkillCaller` (as `skills`). See docs/INTERFACES.md §3.

Every public Robot method is recorded as a PrimitiveCall and passes the SafetyGuard
before anything moves. Keep the public surface in sync with
skills/validator.py:ROBOT_API and droid_template/API.md.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from skillsmith.events import EventBus
from skillsmith.safety import SafetyGuard
from skillsmith.types import PrimitiveCall, SafetyViolation, Scene, SceneObject, SkillError, Unreachable, Vec3, WorldChanged

if TYPE_CHECKING:
    from skillsmith.hal.base import Env

# approach_side (side grasp, gripper level). Measured in the MuJoCo sim on the tapered demo cup.
SIDE_GRIP_BELOW_TOP = 0.02  # m; default grip height under the object's top (narrow end of a tapered cup)
SIDE_HOVER = 0.03           # m; open jaws come in level this far above the object's top, then go down
JAW_CENTRE_SHIFT = 0.03     # m; descend this far along +tool x, then slide back (sim: 0..6 cm pass, -2.5 hits)
SIDE_MIN_REACH = 0.22       # m from the base axis; nearer, a level gripper folds the arm into itself (sim)
WRIST_LOOK_HOVER = 0.04     # m; tip above the top when the wrist camera looks down (object ~mid-image)
WRIST_MAX_FIX = 0.03        # m; a wrist-camera fix larger than this is ignored (wrong camera model)
SWING_CLEARANCE = 0.06      # m; the gripper turns between down and level at least this far above the object
SWING_DPS = 25.0            # deg/s; joint speed cap while the gripper turns (no straight-line tip path then)


class Robot:
    """`robot` inside a skill."""

    def __init__(self, env: Env, guard: SafetyGuard, bus: EventBus, cfg):
        self._env = env
        self._guard = guard
        self._bus = bus
        self._cfg = cfg
        self.trace: list[PrimitiveCall] = []
        self.last_scene: Optional[Scene] = None
        self._side = False  # gripper level instead of pointing down; read from the arm when kin can (_is_level)

    # ---- public API (what skills call) ----------------------------------------------
    def move_to(self, x: float, y: float, z: float, speed: Optional[float] = None,
                side: Optional[bool] = None) -> None:
        """Move the gripper tip to (x, y, z) metres. side=None keeps the current orientation (level
        after approach_side, else pointing down); True = level (side grasp), False = pointing down."""
        args = {"x": float(x), "y": float(y), "z": float(z), "speed": speed, "side": side}

        self._record("move_to", args, lambda i: self._move((float(x), float(y), float(z)), speed, i, side))

    def grasp(self) -> bool:
        """Close the gripper. True if something is held."""
        def do(_i):
            self._env.arm.set_gripper(self._cfg.robot.gripper_closed)
            return self._env.arm.gripper_holding()
        return self._record("grasp", {}, do)

    def release(self) -> None:
        """Open the gripper fully."""
        self._record("release", {}, lambda _i: self._env.arm.set_gripper(self._cfg.robot.gripper_open))

    def look(self) -> Scene:
        """Fresh object detection."""
        def do(_i):
            scene = self._env.perception.observe()
            self.last_scene = scene
            self._bus.emit("scene.observed", scene=scene, image_path=scene.image_path)
            return scene
        return self._record("look", {}, do)

    def home(self) -> None:
        """Go to the safe home pose."""
        hx, hy, hz = self._cfg.robot.home_xyz

        self._record("home", {}, lambda i: self._move((hx, hy, hz), None, i, side=False))

    def position(self) -> Vec3:
        """Current tip position (read-only)."""
        return self._tip()

    def holding(self) -> bool:
        """True if the gripper senses an object (read-only)."""
        return self._env.arm.gripper_holding()

    def note(self, text: str) -> None:
        """Show a short message on the dashboard."""
        def do(_i):
            self._bus.emit("note", text=str(text)[:200])  # returns None: no event dict in the trace
        self._record("note", {"text": str(text)[:200]}, do)

    def align_over(self, obj: str, hover: float = 0.08) -> float:
        """Hover `hover` m above an object's top, using its LATEST position, then fine-align
        with the wrist camera if the perception backend supports it. Returns the remaining
        xy error estimate in metres (0.0 when no fine alignment is available)."""
        def do(i):
            scene = self._env.perception.observe()
            self.last_scene = scene
            target = scene.require(obj)
            self._move((target.x, target.y, target.top + hover), None, i)
            fine = getattr(self._env.perception, "align_offset", None)
            if not callable(fine):
                return 0.0
            dx, dy = fine(target)  # WS3.7: wrist-camera correction in metres
            x, y, z = self._tip()
            self._move((x + dx, y + dy, z), None, i)
            return round(math.hypot(dx, dy), 4)
        return self._record("align_over", {"obj": obj, "hover": hover}, do)

    def approach_side(self, obj: str, grip_z: Optional[float] = None, hover: float = SIDE_HOVER) -> float:
        """Side grasp approach: open the jaws, fix the object's position with the wrist camera (looking
        down from just above it, real arm only), bring the gripper in LEVEL (parallel to the table, jaws
        opening sideways) above it, lower the open jaws around it to `grip_z` (default 2 cm under its
        top) and slide the fixed jaw up to it. Call grasp() next; later move_to calls stay level.
        Returns the wrist-camera correction in metres (0.0 when none was made)."""
        def do(i):
            self._env.arm.set_gripper(self._cfg.robot.gripper_open)
            scene = self._env.perception.observe()
            self.last_scene = scene
            target = scene.require(obj)
            x, y, fix = target.x, target.y, 0.0
            located = self._wrist_locate(target, i)
            if located is not None:
                fix = math.hypot(located[0] - x, located[1] - y)
                x, y = located
            gz = target.top - SIDE_GRIP_BELOW_TOP if grip_z is None else float(grip_z)
            gz = min(max(gz, target.z + 0.01), target.top - 0.005)
            q = None if math.hypot(x, y) < SIDE_MIN_REACH else self._ik((x, y, gz), self._env.arm.joints(), side=True)
            if q is None:  # one solve above: reach check + jaw axis
                raise Unreachable(f"no level (side) grasp of {target.label} at ({x:.3f}, {y:.3f}, z={gz:.3f}), "
                                  f"{math.hypot(x, y):.3f} m from the base: a level gripper reaches about "
                                  "0.23..0.34 m out, less the lower the grip (0.26 m at z=0.075). Move it outward")
            jx, jy, _ = self._jaw_axis(q, x, y)
            sx, sy = x + JAW_CENTRE_SHIFT * jx, y + JAW_CENTRE_SHIFT * jy  # object mid-gap on the way down
            self._move((sx, sy, target.top + max(hover, SWING_CLEARANCE)), None, i, side=True)  # turn up high
            self._move((sx, sy, gz), None, i)
            self._move((x, y, gz), None, i)
            return round(fix, 4)
        return self._record("approach_side", {"obj": obj, "grip_z": grip_z, "hover": hover}, do)

    # ---- internals ------------------------------------------------------------------
    def _ik(self, p: Vec3, seed, side: bool):
        # only pass `approach` when it matters, so simple Kinematics doubles keep working
        return self._env.kin.ik(p, seed, approach="side") if side else self._env.kin.ik(p, seed)

    def _jaw_axis(self, q, x: float, y: float) -> Vec3:
        """The jaws' closing axis (gripper x) at q; tangential when kin cannot say (fake world)."""
        axis = getattr(self._env.kin, "tool_axis", None)
        try:
            if callable(axis):
                return axis(q, "x")
        except AttributeError:  # a wrapper around a kinematics without tool_axis
            pass
        r = math.hypot(x, y) or 1.0
        return (-y / r, x / r, 0.0)

    def _wrist_locate(self, target: SceneObject, index: int) -> Optional[tuple[float, float]]:
        """Look down at `target` from WRIST_LOOK_HOVER above it and locate its top with the wrist
        camera. None (overhead position stands) without a wrist camera, a blob or a sane answer."""
        frame_of = getattr(self._env.perception, "wrist_frame", None)
        pose_of = getattr(self._env.kin, "wrist_camera_pose", None)
        if not callable(frame_of) or not callable(pose_of):
            return None
        from skillsmith.perception import wrist

        try:
            self._move((target.x, target.y, target.top + WRIST_LOOK_HOVER), None, index, side=False)
        except (Unreachable, SafetyViolation) as e:  # the look is optional: grasp on the overhead position
            self._bus.emit("note", text=f"wrist look skipped ({e})"[:200], where="robot.approach_side")
            return None
        frame = frame_of()
        if frame is None:
            return None
        try:
            result = wrist.locate_on_plane(frame, target.color or None, pose_of(self._env.arm.joints()),
                                           target.top, self._cfg.raw())
        except Exception as e:  # cv2 missing, bad frame: never fail the grasp over the fine fix
            self._bus.emit("note", text=f"wrist locate failed ({type(e).__name__}: {e}); overhead position used")
            return None
        fix = math.hypot(result.x - target.x, result.y - target.y) if result.found else 0.0
        ok = result.found and result.confidence >= 0.3 and fix <= WRIST_MAX_FIX
        verdict = "used" if ok else "ignored, overhead position used"
        text = f"wrist locate {target.label}: {result.reason}; fix {fix * 100:.1f} cm ({verdict})"
        self._bus.emit("note", text=text[:200],
                       where="robot.approach_side", x=result.x, y=result.y, fix=round(fix, 4), used=ok)
        return (result.x, result.y) if ok else None

    def _is_level(self) -> bool:
        """Is the gripper level right now? From the arm's joints when kin can tell (survives a failed
        attempt that left the jaws around a cup), else the last commanded orientation."""
        axis = getattr(self._env.kin, "tool_axis", None)
        if not callable(axis):
            return self._side
        try:
            return axis(self._env.arm.joints(), "z")[2] > -0.5
        except AttributeError:
            return self._side

    def _tip(self) -> Vec3:
        x, y, z = self._env.kin.fk(self._env.arm.joints())
        return (round(x, 4), round(y, 4), round(z, 4))

    def _record(self, name: str, args: dict, fn: Callable[[int], Any]) -> Any:
        index = self._guard.count_call()
        call = PrimitiveCall(index=index, name=name, args=args, t=time.time(), ee_before=self._tip())
        try:
            self._check_world()
            call.result = fn(index)
            return call.result
        except Exception as e:
            call.ok = False
            call.error = f"{type(e).__name__}: {e}"
            raise
        finally:
            call.ee_after = self._tip()
            self.trace.append(call)
            self._bus.emit("primitive.call", **_call_dict(call))

    def _check_world(self) -> None:
        """Stop before the next motion if the live world model saw an unexpected change
        (WS3.5 WorldModel.unexpected_motion, e.g. 'red cup moved 3.2 cm while not held')."""
        check = getattr(self._env.perception, "unexpected_motion", None)
        message = check() if callable(check) else None
        if message:
            self._bus.emit("world.bumped", message=message)
            raise WorldChanged(message)

    def _move(self, target: Vec3, speed: Optional[float], index: int, side: Optional[bool] = None) -> None:
        """side=None keeps the gripper's current orientation; the flag only changes once a step succeeds."""
        level = self._is_level()
        want = level if side is None else bool(side)
        if want != level and self._env.arm.gripper_holding():
            raise SkillError("turning the gripper between level and pointing down while holding would tip the "
                             "object over: release it first (or keep the orientation with side=None)")
        self._guard.check_target(target, index)
        v = self._guard.clamp_speed(speed)
        if want and math.hypot(target[0], target[1]) < SIDE_MIN_REACH:
            raise Unreachable(f"({target[0]:.3f}, {target[1]:.3f}) is {math.hypot(target[0], target[1]):.3f} m "
                              f"from the base: with the gripper level it must stay {SIDE_MIN_REACH:.2f} m or more out")
        turning = want != level
        for wp in self._waypoints(self._tip(), target):
            seed = self._env.arm.joints()
            q = self._ik(wp, seed, want)
            if q is None:
                how = " with the gripper level" if want else ""
                raise Unreachable(f"no IK solution for ({wp[0]:.3f}, {wp[1]:.3f}, {wp[2]:.3f}){how}")
            dps = self._joint_speed(seed, q, wp, v)
            self._env.arm.move_joints(q, min(dps, SWING_DPS) if turning else dps)
            turning = False
            self._side = want

    def _waypoints(self, start: Vec3, end: Vec3) -> list[Vec3]:
        """Mostly-vertical moves become straight lines in approach_step chunks."""
        dx, dy, dz = (end[i] - start[i] for i in range(3))
        if abs(dz) <= math.hypot(dx, dy) or abs(dz) <= self._cfg.safety.approach_step:
            return [end]
        n = math.ceil(abs(dz) / self._cfg.safety.approach_step)
        return [tuple(start[i] + (end[i] - start[i]) * k / n for i in range(3)) for k in range(1, n + 1)]

    def _joint_speed(self, q0, q1, wp: Vec3, tcp_speed: float) -> float:
        dist = math.dist(self._tip(), wp)
        duration = max(dist / tcp_speed, 0.05)
        max_delta = max(abs(a - b) for a, b in zip(q0[:5], q1[:5]))
        return float(min(max(max_delta / duration, 5.0), self._cfg.safety.max_joint_speed_dps))


class SkillCaller:
    """`skills` inside a skill: call another saved skill with the same robot."""

    def __init__(self, library: dict, robot: Robot, scene: Scene, depth: int, max_depth: int,
                 runner: Callable[..., Any]):
        self._library = library
        self._robot = robot
        self._scene = scene
        self._depth = depth
        self._max_depth = max_depth
        self._runner = runner

    def call(self, name: str, **params: Any) -> Any:
        from skillsmith.types import SkillError

        if name not in self._library:
            raise SkillError(f"no saved skill named '{name}' (have: {', '.join(sorted(self._library))})")
        if self._depth >= self._max_depth:
            raise SkillError(f"skill nesting deeper than {self._max_depth}")
        self._robot.note(f"→ {name}({', '.join(f'{k}={v!r}' for k, v in params.items())})")
        child = SkillCaller(self._library, self._robot, self._scene, self._depth + 1,
                            self._max_depth, self._runner)
        scene = self._robot.last_scene or self._scene
        return self._runner(self._library[name], name, self._robot, scene, child, params)


def _call_dict(call: PrimitiveCall) -> dict:
    return {
        "index": call.index, "name": call.name, "args": call.args, "t": call.t,
        "ee_before": call.ee_before, "ee_after": call.ee_after, "ok": call.ok,
        "result": call.result if isinstance(call.result, (bool, int, float, str, type(None))) else "…",
        "error": call.error,
    }
