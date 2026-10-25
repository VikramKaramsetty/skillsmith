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
from skillsmith.types import PrimitiveCall, Scene, SceneObject, SkillError, Unreachable, Vec3, WorldChanged

if TYPE_CHECKING:
    from skillsmith.hal.base import Env

# approach_side (side grasp, gripper level). Measured in the MuJoCo sim on the tapered demo cup.
SIDE_GRIP_BELOW_TOP = 0.025  # m; default grip height under the object's top (deep enough to hold it)
SIDE_GRASP_DEPTH = 0.025    # m; the grasp point (fingertips) ends this far PAST the object's centre: closing
#                             on a cup at the fingertips squeezed it out forward on the real arm (the sim
#                             attaches on contact, so it never showed)
SIDE_GRASP_DEPTH_MIN = 0.015  # m; shallower near the reach limit, never less than this
SIDE_TILTS = (10.0, 20.0, 35.0)  # deg below level approach_side allows, tried in order (35 = cups near the base)
SIDE_STANDOFF = 0.06        # m; the level jaws start this far short of the object, then slide in
SIDE_STANDOFF_MARGIN = 0.02  # m; ...and never closer than its radius + this (1 cm let the descent clip the cup)
SIDE_HOVER = 0.03           # m; open jaws come in level this far above the object's top, then go down
JAW_CENTRE_SHIFT = 0.03     # m; descend this far along +tool x, then slide back (sim: 0..6 cm pass, -2.5 hits)
SIDE_MIN_REACH = 0.18       # m from the base axis; nearer, a side pose folds the arm into itself (sim)
WRIST_MAX_FIX = 0.03        # m; a wrist-camera fix larger than this is ignored (wrong camera model)
SWING_CLEARANCE = 0.06      # m; the gripper turns between down and level at least this far above the object
NEAR_DPS = 12.0             # deg/s; joint speed cap for approach_side's moves next to the object (descent,
#                             slide, close-up): a 1.7 cm tip step swung shoulder+wrist 19 deg at 57 deg/s and
#                             shoved the cup on the real arm
DEFAULT_TILT_CAP_DEG = 10.0  # kinematics.SIDE_MAX_TILT_DEG: the side solve's tilt cap when none is given
HELD_TILT_SLACK_DEG = 3.0  # carrying, the cap sits this far above the tilt the object was gripped with
MAX_STEP_JUMP_DEG = 25.0    # a non-turning waypoint moving any joint more than this = IK branch flip, refused
PUSH_TOL = 0.015            # m; the camera seeing the object this far from where the approach planned = pushed
PUSH_TOL_OVER = 0.04        # m; push tolerance when the gripper is over the object (occludes its top)
PUSH_SETTLE_S = 0.4         # s; wait this long after stopping so the overhead frame shows the result
CAPTURE_CALLS = {"approach_side", "align_over", "grasp", "move_to", "release", "home"}  # keyframe after these
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
        self._tilt_cap = SIDE_TILTS[0]  # deg the fingers may point below level in side moves (approach_side sets it)
        self.captures: dict = {}  # call index -> (front BGR, wrist BGR, tip): phase keyframes for Droid

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
            self._env.arm.set_gripper(self._close_pct())
            held = self._env.arm.gripper_holding()
            state = getattr(self._env.arm, "state", None)
            if callable(state):  # evidence: where the jaws stopped and how hard they push
                try:
                    st = state()
                    self._bus.emit("note", text=f"grasp: jaws stopped at {float(st.get('gripper_pos', 0)):.1f} %, "
                                   f"load {float(st.get('gripper_load', 0)):.0f} -> {'holding' if held else 'empty'}",
                                   where="robot.grasp")
                except Exception:
                    pass
            return held
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

    def approach_side(self, obj: str, grip_z: Optional[float] = None, hover: float = SIDE_HOVER,
                      speed: Optional[float] = None) -> float:
        """Side grasp approach, like a hand reaching for a cup: open the jaws, turn the gripper LEVEL
        (parallel to the table, jaws opening sideways) high up behind the object (on the base side),
        lower it to `grip_z` (default 1.5 cm under the top) a few cm short of the object, fix the
        object's position with the wrist camera from there (real arm; sim renders it), then slide in
        horizontally until the open jaws are around it and the fixed jaw touches it. Call grasp()
        next; later move_to calls stay level. Returns the wrist-camera correction in metres."""
        def do(i):
            scene = self._env.perception.observe()
            self.last_scene = scene
            target = scene.require(obj)
            self._grasp_kind = target.kind
            self._env.arm.set_gripper(self._open_pct())  # only as wide as the object needs: neighbours stay clear
            x, y = target.x, target.y
            gz = target.top - SIDE_GRIP_BELOW_TOP if grip_z is None else float(grip_z)
            gz = min(max(gz, target.z + 0.01), target.top - 0.005)
            turn_zs = [target.top + c for c in (max(hover, SWING_CLEARANCE), 0.045) if c <= max(hover, SWING_CLEARANCE)]
            plan, why = None, ""
            for cap in SIDE_TILTS:  # level if the arm can, fingers tilted down further only when it must
                self._tilt_cap = cap
                q = None if math.hypot(x, y) < SIDE_MIN_REACH else self._ik((x, y, gz), self._env.arm.joints(), side=True)
                if q is None:
                    why = f"no side grasp pose within {cap:.0f} deg of level"
                    continue
                (jx, jy, _), (ax, ay) = self._jaw_axis(q, x, y), self._reach_dir(q, x, y)
                shift = (JAW_CENTRE_SHIFT * jx, JAW_CENTRE_SHIFT * jy)  # object mid-gap while the jaws come in
                try:
                    plan = self._side_plan(target, x, y, gz, turn_zs, shift, (ax, ay), q)
                    break
                except Unreachable as e:
                    why = str(e)
            if plan is None:  # right in front of the base: the gripper can only point down there
                return self._approach_down(target, x, y, gz, speed, i, why)
            pts, qs = plan
            if self._tilt_cap > SIDE_TILTS[0]:
                self._bus.emit("note", text=f"side grasp of {target.label} with the fingers up to {self._tilt_cap:.0f} "
                               "deg below level (too near the base for level)", where="robot.approach_side")
            self._play(pts[:1], qs[:1], i, SWING_DPS)      # turn level behind it (lower turns clip its top)
            fix = 0.0
            located = self._wrist_locate(target)            # from up here the camera sees all of its top
            if located is not None:
                new = self._side_path(self._side_points(located[0], located[1], gz, pts[0][2], shift, (ax, ay),
                                                        self._back), self._env.arm.joints(), q)
                if new[1] is not None:
                    fix = math.hypot(located[0] - x, located[1] - y)
                    pts, qs = new
                else:  # no smooth path to the corrected spot: keep the checked plan
                    self._bus.emit("note", text="wrist fix ignored: no smooth path to it", where="robot.approach_side")
            near_dps = NEAR_DPS if speed is None else max(2.0, min(NEAR_DPS, float(speed)))
            n_down = next(k for k in range(1, len(pts)) if abs(pts[k][2] - gz) < 1e-6)
            self._play(pts[1:n_down + 1], qs[1:n_down + 1], i, near_dps)  # down behind it, jaws short of it
            over = self._back < 0  # straddling from above: the gripper hides part of its top
            self._check_not_pushed(target, near_dps, "lowering over it" if over else "lowering behind it",
                                   PUSH_TOL_OVER if over else PUSH_TOL)
            self._play(pts[n_down + 1:], qs[n_down + 1:], i, near_dps)     # slide in, fixed jaw up to it
            self._check_not_pushed(target, near_dps, "sliding the jaws around it", PUSH_TOL_OVER if over else PUSH_TOL)
            return round(fix, 4)
        return self._record("approach_side", {"obj": obj, "grip_z": grip_z, "hover": hover, "speed": speed}, do)

    def _capture(self, index: int, tip: Vec3) -> None:
        grab = getattr(self._env.perception, "keyframe_images", None)
        if not callable(grab):
            return
        try:
            front, wrist = grab()
        except Exception:
            return
        if front is not None or wrist is not None:
            try:
                grip = float(self._env.arm.joints()[5])
            except Exception:
                grip = None
            self.captures[index] = (front, wrist, tip, grip)

    def _open_pct(self) -> float:
        """How far approach_side opens the jaws: robot.gripper_open_by_kind for the target's kind (wide open
        jaws, 13 cm, swept into a neighbouring cup), else gripper_open."""
        by_kind = self._cfg.robot.get("gripper_open_by_kind")
        kinds = by_kind.raw() if by_kind is not None else {}
        return float(kinds.get(getattr(self, "_grasp_kind", None), self._cfg.robot.gripper_open))

    def _close_pct(self) -> float:
        """Where grasp() closes the jaws: robot.gripper_closed_by_kind for the object the last approach
        aimed at (taught per object: a paper cup is gripped at its width, not crushed), else gripper_closed."""
        by_kind = self._cfg.robot.get("gripper_closed_by_kind")
        kinds = by_kind.raw() if by_kind is not None else {}
        kind = getattr(self, "_grasp_kind", None)
        return float(kinds.get(kind, self._cfg.robot.gripper_closed))

    def _check_not_pushed(self, target: SceneObject, dps: float, phase: str, tol: float = PUSH_TOL) -> None:
        """Stop BEFORE closing when the camera sees the object pushed: the gripper touched it. `tol` is
        wider when the gripper is right over the object (it hides part of the top from the overhead camera
        and shifts its estimate ~2 cm: a straddle on the real arm stopped on that false alarm)."""
        moved = getattr(self._env.perception, "displacement", None)
        if not callable(moved):
            return
        if self._env.name == "real":
            time.sleep(PUSH_SETTLE_S)  # a few camera frames after the arm stopped
        d = moved(target.id, (target.x, target.y))
        if d is not None and d > tol:
            self._env.arm.stop()
            raise WorldChanged(f"{target.label} was pushed {d * 100:.1f} cm by the gripper while {phase} "
                               f"(joints at up to {dps:.0f} deg/s): the arm touched it before the grasp. "
                               f"Approach slower, e.g. approach_side(obj, speed={max(2, int(dps / 2))})")

    # ---- internals ------------------------------------------------------------------
    def _ik(self, p: Vec3, seed, side: bool, hold: bool = False):
        # only pass `approach` when it matters, so simple Kinematics doubles keep working
        if not side:
            return self._env.kin.ik(p, seed)
        if getattr(self._env.kin, "tool_axis", None) is None:  # fake world / doubles: plain side solve
            return self._env.kin.ik(p, seed, approach="side")
        cap = self._tilt_cap
        if hold:  # keep_tilt holds the tilt it gripped with, so the cap must allow it: an 11 deg grip under the
            down = max(-1.0, min(1.0, -self._env.kin.tool_axis(seed, "z")[2]))  # 10 deg default had no solution
            cap = max(cap if cap is not None else DEFAULT_TILT_CAP_DEG, math.degrees(math.asin(down)) + HELD_TILT_SLACK_DEG)
        return self._env.kin.ik(p, seed, approach="side", keep_tilt=hold, side_max_tilt=cap)

    def _axis_or(self, q, name: str, fallback: Vec3) -> Vec3:
        axis = getattr(self._env.kin, "tool_axis", None)
        try:
            if callable(axis):
                return axis(q, name)
        except AttributeError:  # a wrapper around a kinematics without tool_axis
            pass
        return fallback

    def _jaw_axis(self, q, x: float, y: float) -> Vec3:
        """The jaws' closing axis (gripper x) at q; tangential when kin cannot say (fake world)."""
        r = math.hypot(x, y) or 1.0
        return self._axis_or(q, "x", (-y / r, x / r, 0.0))

    def _reach_dir(self, q, x: float, y: float) -> tuple[float, float]:
        """Horizontal unit direction a level gripper points at q (away from the base)."""
        r = math.hypot(x, y) or 1.0
        zx, zy, _ = self._axis_or(q, "z", (x / r, y / r, 0.0))
        n = math.hypot(zx, zy) or 1.0
        return zx / n, zy / n

    def _side_points(self, x: float, y: float, gz: float, turn_z: float, shift, a, back: float) -> list:
        """Tip points of a side approach in execution order: turn pose behind the object, approach_step
        steps down to grip height, a straight slide in until the fingertips are SIDE_GRASP_DEPTH past the
        object's centre (jaws either side of it), then the fixed jaw over to it."""
        step = self._cfg.safety.approach_step
        bx, by = x + shift[0] - back * a[0], y + shift[1] - back * a[1]
        depth = self._grasp_depth(x, y, shift)
        dx, dy = depth * a[0], depth * a[1]
        sx, sy = x + shift[0] + dx, y + shift[1] + dy
        n_down = max(1, math.ceil((turn_z - gz) / step))
        n_in = max(1, math.ceil(math.hypot(sx - bx, sy - by) / step))
        pts = [(bx, by, turn_z)]
        pts += [(bx, by, turn_z + (gz - turn_z) * k / n_down) for k in range(1, n_down + 1)]
        pts += [(bx + (sx - bx) * k / n_in, by + (sy - by) * k / n_in, gz) for k in range(1, n_in + 1)]
        pts.append((x + dx, y + dy, gz))
        return pts

    def _approach_down(self, target: SceneObject, x: float, y: float, gz: float, speed, i: int, why: str) -> float:
        """Fallback for objects right in front of the base, where a sideways gripper cannot reach: point
        the gripper straight down, come down over the object with the open jaws straddling its top (object
        mid-gap), then close the gap to the fixed jaw. Same speed cap and push checks as the side approach."""
        q = self._ik((x, y, gz), self._env.arm.joints(), side=False)
        if q is None:
            raise Unreachable(f"no grasp of {target.label} at ({x:.3f}, {y:.3f}): no side approach ({why}) and "
                              "no top-down one either")
        jx, jy, _ = self._jaw_axis(q, x, y)
        sx, sy = x + JAW_CENTRE_SHIFT * jx, y + JAW_CENTRE_SHIFT * jy
        near_dps = NEAR_DPS if speed is None else max(2.0, min(NEAR_DPS, float(speed)))
        self._bus.emit("note", text=f"{target.label} is right in front of the base: grasping it from above, gripper "
                       "pointing down, jaws straddling its top", where="robot.approach_side")
        self._move((sx, sy, target.top + SWING_CLEARANCE), None, i, side=False, max_dps=SWING_DPS)
        self._move((sx, sy, gz), None, i, max_dps=near_dps)
        self._check_not_pushed(target, near_dps, "lowering the open jaws over it", PUSH_TOL_OVER)
        self._move((x, y, gz), None, i, max_dps=near_dps)
        self._check_not_pushed(target, near_dps, "closing the gap to the fixed jaw", PUSH_TOL_OVER)
        return 0.0

    def _grasp_depth(self, x: float, y: float, shift) -> float:
        """SIDE_GRASP_DEPTH, or less (not below SIDE_GRASP_DEPTH_MIN) so the fingertips stay inside
        workspace.max_reach with a few mm to spare."""
        room = float(self._cfg.workspace.max_reach) - 0.003 - math.hypot(x + shift[0], y + shift[1])
        return max(SIDE_GRASP_DEPTH_MIN, min(SIDE_GRASP_DEPTH, room))

    def _side_path(self, pts: list, start, grasp_q):
        """Joint poses for `pts`, solved BACKWARD from the grasp pose (each from the next one), so the whole
        approach stays on the grasp's arm configuration; then checked forward for joint jumps, including
        from `start` (where the arm is) to the first pose only for the non-turning case. (pts, qs) or
        (pts, None) when some step is unreachable or would swing more than MAX_STEP_JUMP_DEG."""
        qs = [None] * len(pts)
        seed = grasp_q
        for k in range(len(pts) - 1, -1, -1):
            qk = self._ik(pts[k], seed, side=True)
            if qk is None:
                return pts, None
            if k < len(pts) - 1 and max(abs(a - b) for a, b in zip(qk[:5], qs[k + 1][:5])) > MAX_STEP_JUMP_DEG:
                return pts, None
            qs[k], seed = qk, qk
        return pts, qs

    def _side_plan(self, target: SceneObject, x: float, y: float, gz: float, turn_zs, shift, a, grasp_q):
        """The longest standoff (SIDE_STANDOFF down to radius + margin) and highest turn height with a
        smooth joint path; Unreachable when there is none."""
        least = target.width_at(gz) / 2 + SIDE_STANDOFF_MARGIN
        back = SIDE_STANDOFF
        while back >= least:
            for turn_z in turn_zs:
                pts = self._side_points(x, y, gz, turn_z, shift, a, back)
                if min(math.hypot(p[0], p[1]) for p in pts) < SIDE_MIN_REACH:
                    continue
                pts, qs = self._side_path(pts, self._env.arm.joints(), grasp_q)
                if qs is not None:
                    self._back = back
                    return pts, qs
            back -= 0.005
        # no room to start behind it (nearer than ~0.29 m): come straight down with the open jaws straddling
        # its narrow top, already at the grasp depth (the slide-in has length 0)
        back = -self._grasp_depth(x, y, shift)
        for turn_z in turn_zs:
            pts = self._side_points(x, y, gz, turn_z, shift, a, back)
            if min(math.hypot(p[0], p[1]) for p in pts) < SIDE_MIN_REACH:
                continue
            pts, qs = self._side_path(pts, self._env.arm.joints(), grasp_q)
            if qs is not None:
                self._back = back
                self._bus.emit("note", text=f"{target.label}: no room to reach in from the side; coming down over it "
                               "with the jaws straddling its top", where="robot.approach_side")
                return pts, qs
        raise Unreachable(f"{target.label} at ({x:.3f}, {y:.3f}): no smooth side approach, neither reaching in "
                          f"(needs {least * 100:.0f} cm behind it) nor straddling it from above")

    def _play(self, pts: list, qs: list, index: int, max_dps: float) -> None:
        """Execute planned joint poses: safety-check each tip point, cap the joint speed, stay level."""
        v = self._guard.clamp_speed(None)
        for p, q in zip(pts, qs):
            self._guard.check_target(p, index)
            q = list(q[:5]) + [self._env.arm.joints()[5]]
            dps = min(self._joint_speed(self._env.arm.joints(), q, p, v), max_dps)
            self._env.arm.move_joints(q, dps)
            self._side = True

    def _wrist_locate(self, target: SceneObject) -> Optional[tuple[float, float]]:
        """Locate `target`'s top with the wrist camera from the current pose. None (overhead position
        stands) without a wrist camera, a blob or a sane answer."""
        frame_of = getattr(self._env.perception, "wrist_frame", None)
        pose_of = getattr(self._env.kin, "wrist_camera_pose", None)
        if not callable(frame_of) or not callable(pose_of):
            return None
        from skillsmith.perception import wrist

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
        partial = "image edge" in result.reason  # a cut-off blob's centroid is off by a cm or more
        ok = result.found and not partial and result.confidence >= 0.3 and fix <= WRIST_MAX_FIX
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
            if name in CAPTURE_CALLS:
                self._capture(index, call.ee_after)
            self._bus.emit("primitive.call", **_call_dict(call))

    def _check_world(self) -> None:
        """Stop before the next motion if the live world model saw an unexpected change
        (WS3.5 WorldModel.unexpected_motion, e.g. 'red cup moved 3.2 cm while not held')."""
        check = getattr(self._env.perception, "unexpected_motion", None)
        message = check() if callable(check) else None
        if message:
            self._bus.emit("world.bumped", message=message)
            raise WorldChanged(message)

    def _move(self, target: Vec3, speed: Optional[float], index: int, side: Optional[bool] = None,
              max_dps: Optional[float] = None, seed: Optional[list] = None) -> None:
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
        hold = want and not turning  # keep the tilt on any level move: a cap under the current tilt flips the branch
        prev = seed  # each waypoint is solved from the previous solution (like _descent_ok), not the measured
        for wp in self._waypoints(self._tip(), target, straight=want and not turning):  # joints: near a limit,
            seed = prev if prev is not None else self._env.arm.joints()                  # sag can flip the branch
            q = self._ik(wp, seed, want, hold)
            prev = q
            if q is None:
                how = " with the gripper level" if want else ""
                raise Unreachable(f"no IK solution for ({wp[0]:.3f}, {wp[1]:.3f}, {wp[2]:.3f}){how}")
            jump = max(abs(a - b) for a, b in zip(q[:5], self._env.arm.joints()[:5]))
            short = math.dist(self._tip(), wp) <= self._cfg.safety.approach_step * 1.05
            if want and not turning and short and jump > MAX_STEP_JUMP_DEG:  # IK switched configuration: a swing
                raise Unreachable(f"no smooth path to ({wp[0]:.3f}, {wp[1]:.3f}, {wp[2]:.3f}): the arm would "
                                  f"swing {jump:.0f} deg to another configuration on a short step")
            dps = self._joint_speed(self._env.arm.joints(), q, wp, v)
            if turning:
                dps = min(dps, SWING_DPS)
            if max_dps is not None:
                dps = min(dps, max_dps)
            self._env.arm.move_joints(q, dps)
            turning = False
            self._side = want

    def _waypoints(self, start: Vec3, end: Vec3, straight: bool = False) -> list[Vec3]:
        """Mostly-vertical moves (any move with `straight`, e.g. a level slide toward a cup) become
        straight lines in approach_step chunks."""
        dx, dy, dz = (end[i] - start[i] for i in range(3))
        step = self._cfg.safety.approach_step
        if straight:
            n = max(1, math.ceil(math.dist(start, end) / step))
            return [tuple(start[i] + (end[i] - start[i]) * k / n for i in range(3)) for k in range(1, n + 1)]
        if abs(dz) <= math.hypot(dx, dy) or abs(dz) <= step:
            return [end]
        n = math.ceil(abs(dz) / step)
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
