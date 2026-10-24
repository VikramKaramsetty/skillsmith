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
from skillsmith.types import PrimitiveCall, Scene, Unreachable, Vec3, WorldChanged

if TYPE_CHECKING:
    from skillsmith.hal.base import Env


class Robot:
    """`robot` inside a skill."""

    def __init__(self, env: Env, guard: SafetyGuard, bus: EventBus, cfg):
        self._env = env
        self._guard = guard
        self._bus = bus
        self._cfg = cfg
        self.trace: list[PrimitiveCall] = []
        self.last_scene: Optional[Scene] = None

    # ---- public API (what skills call) ----------------------------------------------
    def move_to(self, x: float, y: float, z: float, speed: Optional[float] = None) -> None:
        """Move the gripper tip to (x, y, z) metres, pointing down."""
        args = {"x": float(x), "y": float(y), "z": float(z), "speed": speed}
        self._record("move_to", args, lambda i: self._move((float(x), float(y), float(z)), speed, i))

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
        self._record("home", {}, lambda i: self._move((hx, hy, hz), None, i))

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

    # ---- internals ------------------------------------------------------------------
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

    def _move(self, target: Vec3, speed: Optional[float], index: int) -> None:
        self._guard.check_target(target, index)
        v = self._guard.clamp_speed(speed)
        for wp in self._waypoints(self._tip(), target):
            seed = self._env.arm.joints()
            q = self._env.kin.ik(wp, seed)
            if q is None:
                raise Unreachable(f"no IK solution for ({wp[0]:.3f}, {wp[1]:.3f}, {wp[2]:.3f})")
            self._env.arm.move_joints(q, self._joint_speed(seed, q, wp, v))

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
