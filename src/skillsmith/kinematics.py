"""Shared kinematics for the real arm AND the sim. OWNER: WS2 (docs/workstreams/WS2_kinematics_sim.md).

Contract (docs/INTERFACES.md §2, `Kinematics` protocol):
- `PlacoKinematics(urdf_path, ee_frame="gripper_frame_link", joint_names=<5 arm joints>)`.
- `fk(joints) -> (x, y, z)`: gripper tip in the robot base frame, metres. `joints` has 6 values:
  5 arm joints in degrees (LeRobot calibrated, use_degrees=True) + gripper 0..100 (ignored).
- `ik(target, seed, approach="down") -> 6 joints | None`: tip at `target` with the gripper pointing
  straight down, solved from `seed`; the gripper value is copied from `seed[5]`. Return None if the
  remaining position error is > 5 mm or a joint limit would be crossed. `approach="side"` holds the
  gripper LEVEL instead (see "Side approach" below).
- Use placo directly (extra `kin`); do NOT import LeRobot here (it pulls in torch). Import placo
  lazily inside `__init__` so `make check` runs without it.
- This one class is used by hal/real.py and hal/sim.py, so the sim gate validates the real IK.
- Verify joint signs/offsets against the real arm in WS2.7 before trusting it on hardware.

The tip is the GRASP POINT, not the URDF frame itself (WS2.4):
- `gripper_frame_link` in so101_new_calib.urdf sits on the fixed jaw's tip. Skills (and the fake
  world) put the tip on an object's centre to grasp it, which would drive the fixed jaw into the
  object. So the frame is moved TIP_OFFSET along gripper_link +x (fixed jaw -> moving jaw) in a temp
  copy of the URDF, and fk/ik use that point. Pointing down, the move is horizontal, so heights
  (and safety.min_tip_z) mean the same as before.
- 0.035 m is measured in MuJoCo on the demo table: the fixed jaw clears a 6.5 cm cup by ~2.5 mm
  (3.0 cm hits it), and a 3 cm block is pushed ~2 cm toward the fixed jaw as the moving jaw closes.
  At 4 cm and more, the closing jaw presses the cup ~3 mm into the table and a stack lands > 5 mm
  deep. Objects wider than ~6.5 cm need a larger offset; WS2.7 checks this on the real arm.
- `tip_offset=0` gives the raw URDF frame.

How `ik` solves (WS2.2):
- Position comes first, "pointing down" second. With the SO101 URDF limits (wrist_flex ±95°) the
  tip can only point straight down below about z = 0.08 m: the wrist and gripper are ~0.16 m long,
  so a vertical gripper needs the forearm level or lower. Above that the solver keeps the position
  exact and tilts the gripper as little as the joints allow. `last_tilt_deg` reports the tilt.
  Set `max_tilt_deg` to reject tilted solutions instead.
- wrist_roll is held at `seed[4]` (top-down grasps don't need yaw); the gripper joint is ignored.
- The seed is tried first so consecutive waypoints stay on the same branch. If that does not end
  vertical, a few elbow-up seeds aimed at the target azimuth are tried and the least tilted
  solution wins (ties go to the one closest to the seed).

Side approach (`approach="side"`):
- The gripper is level (parallel to the table) and points AWAY from the base. wrist_roll is held at
  `side_roll_deg` (90) so the jaws open sideways: gripper_link x (the closing axis) is horizontal,
  which is what a side grasp of an upright cup needs.
- "Level" = gripper_link y (normal to the jaw plane) points straight down (up for a negative roll),
  so both the pointing axis and the closing axis are horizontal. Yaw is left to shoulder_pan: a
  5-joint arm points a level gripper parallel to its own vertical plane, and the grasp point sits
  TIP_OFFSET to the side of that plane, so the gripper is NOT aimed exactly at the base axis
  (~8 deg off at r = 0.25 m). Approach along `tool_axis(q)`, not along the radial line: moving the
  tip along the tool axis keeps shoulder_pan constant.
- Same two-phase solve with "level" in place of "pointing down", but the first start that ends
  within SIDE_MAX_TILT_DEG wins (normally the seed), so a side waypoint costs one solve, not seven.
  A side solution tilted more than SIDE_MAX_TILT_DEG is rejected (None): a pitched gripper closes
  on a tapered cup at an angle.
  `last_tilt_deg` reports the tilt. The tip is still the grasp point between the jaws.
- Side solutions keep SIDE_LIMIT_MARGIN_DEG inside the URDF limits: near the base a level gripper
  sits on wrist_flex's -95 deg stop, and robotd refuses goals within 2 deg of a limit.

Wrist camera (`wrist_camera_pose`):
- The camera is rigid on the gripper, so its pose is the tip pose times WRIST_CAM_IN_TIP, measured
  from the vendored MJCF's `wrist_cam` (official SO-101 camera mount). MuJoCo camera convention: it
  looks along its -z, image up is +y. Pointing down, it sits ~5.5 cm to the side of the tip and
  4.5 cm above it, tilted 33 deg back under the tip: an object ~4 cm below the tip is mid-image.
  A camera mounted differently on the real arm needs `wrist.cam_in_tip` (4x4) in robot.yaml.
"""

from __future__ import annotations

import functools
import math
from pathlib import Path
from typing import List, Optional, Sequence

from skillsmith.types import Vec3

ARM_JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll")
TIP_OFFSET = 0.035     # m; grasp point = fixed jaw tip + this toward the moving jaw (see module doc)
MAX_POS_ERR = 0.005    # m; contract: None above this
LIMIT_EPS_DEG = 0.05   # numerical slack on joint limits
VERTICAL_DEG = 0.5     # tilt below this counts as "pointing straight down"
REACH_DOWN_WEIGHT = 1e-5  # phase 1: tip error is in metres; at 1e-5 a 1 rad tilt weighs like 3 mm
WRIST_CAM_IN_TIP = ((-0.99995, 0.00786, 0.00553, 0.03007),   # wrist_cam in the tip frame (from the MJCF)
                    (0.00961, 0.83676, 0.54748, 0.05463),
                    (-0.00032, 0.54751, -0.83680, -0.04504),
                    (0.0, 0.0, 0.0, 1.0))
HARD_PHASE_ERR = 0.02  # m; phase 2 (hard tip task) only starts this close, else its QP is infeasible
STEP_DT = 0.01         # s; placo step = velocity limit * dt, i.e. at most ~6 deg per joint per iteration
# (shoulder_lift, elbow_flex, wrist_flex) in degrees: elbow-up postures with the gripper near vertical
RESTART_POSTURES = ((-40.0, 40.0, 90.0), (0.0, 0.0, 90.0), (20.0, -20.0, 90.0), (-60.0, 80.0, 60.0),
                    (40.0, -40.0, 90.0), (-80.0, 60.0, 90.0))
APPROACHES = ("down", "side")
SIDE_ROLL_DEG = -90.0     # wrist_roll for a side grasp: jaws open sideways, wrist camera on TOP (+90 hangs it under)
SIDE_LIMIT_MARGIN_DEG = 3.0  # side solutions stay this far inside the URDF limits (robotd refuses < 2 deg)
SIDE_MAX_TILT_DEG = 10.0  # a "level" gripper may pitch this much (wrist_flex limit near the base), else None
# (shoulder_lift, elbow_flex, wrist_flex): postures with the gripper level, low and far to near
SIDE_POSTURES = ((0.0, 60.0, -60.0), (20.0, 30.0, -50.0), (-20.0, 80.0, -60.0), (40.0, 0.0, -40.0),
                 (30.0, 40.0, -70.0), (-40.0, 90.0, -50.0))


class PlacoKinematics:
    def __init__(self, urdf_path: str, ee_frame: str = "gripper_frame_link",
                 joint_names: Optional[Sequence[str]] = None, max_tilt_deg: Optional[float] = None,
                 iterations: int = 100, tip_offset: float = TIP_OFFSET,
                 side_roll_deg: float = SIDE_ROLL_DEG, cam_in_tip: Optional[Sequence[Sequence[float]]] = None):
        import numpy as np
        import placo

        if not Path(urdf_path).is_file():  # NotImplementedError = "WS2 not ready" for the conformance suite
            raise NotImplementedError(f"SO101 URDF not vendored at {urdf_path}: "
                                      "run python assets/so101/fetch_so101.py (WS2.1)")
        self.tip_offset = tip_offset
        if tip_offset:
            urdf_path = _shifted_urdf(Path(urdf_path), ee_frame, tip_offset)
        self._np = np
        self.ee_frame = ee_frame
        self.joint_names = list(joint_names or ARM_JOINTS)
        if len(self.joint_names) != 5:
            raise ValueError(f"expected 5 arm joint names, got {self.joint_names}")
        self.max_tilt_deg = max_tilt_deg
        self.iterations = iterations
        self.last_tilt_deg: Optional[float] = None  # tilt of the last ik solution from its approach axis
        self.side_roll_deg = float(side_roll_deg)
        self._cam_in_tip = np.asarray(cam_in_tip if cam_in_tip is not None else WRIST_CAM_IN_TIP, dtype=float)
        if self._cam_in_tip.shape != (4, 4):
            raise ValueError(f"cam_in_tip must be 4x4, got shape {self._cam_in_tip.shape}")

        self._robot = placo.RobotWrapper(str(urdf_path), placo.Flags.ignore_collisions)
        self._limits = {n: tuple(math.degrees(v) for v in self._robot.get_joint_limits(n))
                        for n in self.joint_names}
        self._origin = self._pan_axis_origin()
        self._solver = self._make_solver(placo)
        self._pan_sign = self._measure_pan_sign()
        self._side_level_z = self._measure_side_level()

    # ---- Kinematics protocol ------------------------------------------------------------
    def fk(self, joints: Sequence[float]) -> Vec3:
        self._set_arm(joints)
        x, y, z = self._tip_pose()[:3, 3] - self._origin
        return (float(x), float(y), float(z))

    def ik(self, target: Vec3, seed: Sequence[float], approach: str = "down") -> Optional[List[float]]:
        if approach not in APPROACHES:
            raise ValueError(f"approach must be one of {APPROACHES}, got {approach!r}")
        target_arr = self._np.asarray(target, dtype=float) + self._origin  # robot frame -> URDF base_link
        self._pos_task.target_world = target_arr
        self._col, self._axis = self._approach_axis(approach)
        self._margin = SIDE_LIMIT_MARGIN_DEG if approach == "side" else 0.0
        self._down_task.axis_frame = self._np.eye(3)[self._col]
        self._down_task.targetAxis_world = self._axis
        best = None
        for start in self._starts(target, seed, approach):
            cand = self._solve(target_arr, start, seed)
            if cand is not None and (best is None or cand[:2] < best[:2]):
                best = cand
            if best is not None and best[0] < (SIDE_MAX_TILT_DEG if approach == "side" else VERTICAL_DEG):
                break  # side: the first level-enough start wins (usually the seed), no extra solves
        self.last_tilt_deg = None if best is None else best[0]
        max_tilt = SIDE_MAX_TILT_DEG if approach == "side" else self.max_tilt_deg
        if best is None or (max_tilt is not None and best[0] > max_tilt):
            return None
        return best[2] + [float(seed[5])]

    def tool_axis(self, joints: Sequence[float], axis: str = "z") -> Vec3:
        """Unit gripper axis in the robot frame at `joints`: "z" = where it points ((0, 0, -1) = straight
        down), "x" = the jaws' closing axis."""
        self._set_arm(joints)
        x, y, z = self._tip_pose()[:3, "xyz".index(axis)]
        return (float(x), float(y), float(z))

    def wrist_camera_pose(self, joints: Sequence[float]):
        """4x4 pose of the wrist camera in the robot frame (MuJoCo camera axes: looks along -z)."""
        self._set_arm(joints)
        pose = self._tip_pose().copy()
        pose[:3, 3] -= self._origin
        return pose @ self._cam_in_tip

    # ---- internals ----------------------------------------------------------------------
    def _make_solver(self, placo):
        np = self._np
        solver = placo.KinematicsSolver(self._robot)
        solver.mask_fbase(True)
        solver.enable_joint_limits(True)
        solver.enable_velocity_limits(True)  # with dt: caps each step (URDF 10 rad/s * dt), so the
        solver.dt = STEP_DT                  # linearised hard tip task never overshoots
        for name in self._robot.joint_names():
            if name not in self.joint_names[:4]:
                solver.mask_dof(name)  # wrist_roll held at the seed, gripper ignored
        self._pos_task = solver.add_position_task(self.ee_frame, np.zeros(3))
        self._pos_task.configure("tip", "soft", 1.0)
        self._down_task = solver.add_axisalign_task(self.ee_frame, np.array([0.0, 0.0, 1.0]),
                                                    np.array([0.0, 0.0, -1.0]))
        self._down_task.configure("down", "soft", REACH_DOWN_WEIGHT)
        self._col, self._axis = 2, np.array([0.0, 0.0, -1.0])  # gripper axis _down_task aligns, and with what
        self._margin = 0.0
        solver.add_regularization_task(1e-4)
        return solver

    def _pan_axis_origin(self):
        """Robot frame origin in URDF base_link coordinates: on the table (base_link z = 0) under the
        shoulder_pan axis. The SO101 URDF puts that axis ~39 mm in front of base_link's origin."""
        self._robot.update_kinematics()
        origin = self._robot.get_T_world_frame(self.joint_names[0])[:3, 3].copy()  # pinocchio joint frame
        origin[2] = 0.0
        return origin

    def _measure_pan_sign(self) -> float:
        """+1 if positive shoulder_pan turns the tip toward +y (WS2.7 may flip it on the real arm)."""
        y = self.fk([10.0, 0.0, 0.0, 90.0, 0.0, 0.0])[1] - self.fk([0.0, 0.0, 0.0, 90.0, 0.0, 0.0])[1]
        return 1.0 if y >= 0 else -1.0

    def _approach_axis(self, approach: str):
        """(gripper frame axis index, world direction it must point): z down, or y down/up (level)."""
        if approach == "down":
            return 2, self._np.array([0.0, 0.0, -1.0])
        return 1, self._np.array([0.0, 0.0, self._side_level_z])

    def _measure_side_level(self) -> float:
        """World z of gripper_link y when the gripper is level, pointing away from the base, at side_roll_deg."""
        self._set_arm([0.0, 0.0, 0.0, 0.0, self.side_roll_deg])
        y_z = float(self._tip_pose()[2, 1])
        if abs(y_z) < 0.9:
            raise ValueError(f"side_roll_deg {self.side_roll_deg} does not open the jaws sideways (|y.z| {y_z:.2f})")
        return 1.0 if y_z > 0 else -1.0

    def _starts(self, target: Vec3, seed: Sequence[float], approach: str = "down"):
        roll = self.side_roll_deg if approach == "side" else float(seed[4])
        yield [*(float(v) for v in seed[:4]), roll]
        pan = self._pan_sign * math.degrees(math.atan2(target[1], target[0]))
        for lift, elbow, wrist in (SIDE_POSTURES if approach == "side" else RESTART_POSTURES):
            yield [pan, lift, elbow, wrist, roll]

    def _solve(self, target, start: List[float], seed: Sequence[float]):
        """Two phases from `start`: (1) reach the target with a weak pull toward vertical,
        (2) hold the tip exactly (hard task) and tilt toward vertical as far as the joints allow.
        Returns (tilt_deg, distance_to_seed, joints), or None if off target or past a limit."""
        self._set_arm(self._clamped(start))
        self._pos_task.configure("tip", "soft", 1.0)
        self._down_task.configure("down", "soft", REACH_DOWN_WEIGHT)
        self._iterate(target, reach_only=True)
        if self._err(target) < HARD_PHASE_ERR:
            self._pos_task.configure("tip", "hard", 1.0)
            self._down_task.configure("down", "soft", 1.0)
            self._iterate(target, reach_only=False)
        pose = self._tip_pose()
        err = self._err(target)
        joints = [math.degrees(self._robot.get_joint(n)) for n in self.joint_names]
        if err > MAX_POS_ERR or not self._within_limits(joints, self._margin):
            return None
        tilt = math.degrees(math.acos(max(-1.0, min(1.0, float(pose[:3, self._col] @ self._axis)))))
        dist = max(abs(a - b) for a, b in zip(joints, seed[:5]))
        return (round(tilt, 3), dist, joints)

    def _iterate(self, target, reach_only: bool) -> None:
        """Solver steps until the tip is on target (reach_only) or the tilt is aligned / settled."""
        prev = None
        for _ in range(self.iterations):
            try:
                self._solver.solve(True)
            except RuntimeError:  # hard tip task infeasible from here: keep the last good state
                break
            self._robot.update_kinematics()
            aligned = float(self._tip_pose()[:3, self._col] @ self._axis)
            if self._err(target) < 1e-4 and (reach_only or aligned > 0.99999
                                             or prev is not None and abs(prev - aligned) < 1e-8):
                break
            prev = aligned

    def _err(self, target) -> float:
        return float(self._np.linalg.norm(self._tip_pose()[:3, 3] - target))

    def _set_arm(self, joints: Sequence[float]) -> None:
        for name, deg in zip(self.joint_names, joints[:5]):
            self._robot.set_joint(name, math.radians(deg))
        self._robot.update_kinematics()

    def _tip_pose(self):
        return self._robot.get_T_world_frame(self.ee_frame)

    def _clamped(self, joints: Sequence[float]) -> List[float]:
        return [min(max(v, self._limits[n][0]), self._limits[n][1]) for n, v in zip(self.joint_names, joints)]

    def _within_limits(self, joints: Sequence[float], margin: float = 0.0) -> bool:
        return all(self._limits[n][0] + margin - LIMIT_EPS_DEG <= v <= self._limits[n][1] - margin + LIMIT_EPS_DEG
                   for n, v in zip(self.joint_names, joints))


@functools.cache  # one temp file per (urdf, frame, offset) per process
def _shifted_urdf(urdf: Path, ee_frame: str, dx: float) -> str:
    """A temp copy of the URDF whose `ee_frame` joint origin is moved dx along its parent's x axis
    (fixed jaw -> moving jaw), with mesh paths made absolute so the copy loads from anywhere."""
    import re
    import tempfile

    text = urdf.read_text()
    joint = re.compile(r'(<joint name="[^"]+" type="fixed">\s*<origin xyz=")(\S+)( [^"]*"[^>]*/>\s*'
                       r'<parent link="[^"]+"/>\s*<child link="' + re.escape(ee_frame) + '"/>)')
    match = joint.search(text)
    if match is None:
        raise ValueError(f"{urdf}: no fixed joint with child link {ee_frame!r} to apply tip_offset to")
    x = float(match.group(2)) + dx
    text = text[:match.start()] + match.group(1) + repr(x) + match.group(3) + text[match.end():]
    text = re.sub(r'filename="(?!/|package://)([^"]+)"', lambda m: f'filename="{urdf.parent.resolve() / m.group(1)}"', text)
    out = tempfile.NamedTemporaryFile("w", suffix=".urdf", prefix="so101_tip_", delete=False)
    with out:
        out.write(text)
    return out.name


def make_kinematics(cfg) -> PlacoKinematics:
    """Factory used by hal/real.py and hal/sim.py."""
    wrist = cfg.get("wrist")
    return PlacoKinematics(str(cfg.path(cfg.robot.urdf)), cfg.robot.ee_frame, cfg.robot.joint_names[:5],
                           side_roll_deg=float(cfg.robot.get("side_roll_deg", SIDE_ROLL_DEG)),
                           cam_in_tip=wrist.get("cam_in_tip") if wrist is not None else None)
