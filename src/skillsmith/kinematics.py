"""Shared kinematics for the real arm AND the sim. OWNER: WS2 (docs/workstreams/WS2_kinematics_sim.md).

Contract (docs/INTERFACES.md §2, `Kinematics` protocol):
- `PlacoKinematics(urdf_path, ee_frame="gripper_frame_link", joint_names=<5 arm joints>)`.
- `fk(joints) -> (x, y, z)`: gripper tip in the robot base frame, metres. `joints` has 6 values:
  5 arm joints in degrees (LeRobot calibrated, use_degrees=True) + gripper 0..100 (ignored).
- `ik(target, seed) -> 6 joints | None`: tip at `target` with the gripper pointing straight down,
  solved from `seed`; the gripper value is copied from `seed[5]`. Return None if the remaining
  position error is > 5 mm or a joint limit would be crossed.
- Use placo directly (extra `kin`); do NOT import LeRobot here (it pulls in torch). Import placo
  lazily inside `__init__` so `make check` runs without it.
- This one class is used by hal/real.py and hal/sim.py, so the sim gate validates the real IK.
- Verify joint signs/offsets against the real arm in WS2.7 before trusting it on hardware.

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
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import List, Optional, Sequence

from skillsmith.types import Vec3

ARM_JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll")
MAX_POS_ERR = 0.005    # m; contract: None above this
LIMIT_EPS_DEG = 0.05   # numerical slack on joint limits
VERTICAL_DEG = 0.5     # tilt below this counts as "pointing straight down"
REACH_DOWN_WEIGHT = 1e-5  # phase 1: tip error is in metres; at 1e-5 a 1 rad tilt weighs like 3 mm
HARD_PHASE_ERR = 0.02  # m; phase 2 (hard tip task) only starts this close, else its QP is infeasible
STEP_DT = 0.01         # s; placo step = velocity limit * dt, i.e. at most ~6 deg per joint per iteration
# (shoulder_lift, elbow_flex, wrist_flex) in degrees: elbow-up postures with the gripper near vertical
RESTART_POSTURES = ((-40.0, 40.0, 90.0), (0.0, 0.0, 90.0), (20.0, -20.0, 90.0), (-60.0, 80.0, 60.0),
                    (40.0, -40.0, 90.0), (-80.0, 60.0, 90.0))


class PlacoKinematics:
    def __init__(self, urdf_path: str, ee_frame: str = "gripper_frame_link",
                 joint_names: Optional[Sequence[str]] = None, max_tilt_deg: Optional[float] = None,
                 iterations: int = 100):
        import numpy as np
        import placo

        if not Path(urdf_path).is_file():  # NotImplementedError = "WS2 not ready" for the conformance suite
            raise NotImplementedError(f"SO101 URDF not vendored at {urdf_path}: "
                                      "run python assets/so101/fetch_so101.py (WS2.1)")
        self._np = np
        self.ee_frame = ee_frame
        self.joint_names = list(joint_names or ARM_JOINTS)
        if len(self.joint_names) != 5:
            raise ValueError(f"expected 5 arm joint names, got {self.joint_names}")
        self.max_tilt_deg = max_tilt_deg
        self.iterations = iterations
        self.last_tilt_deg: Optional[float] = None  # tilt of the last ik solution from vertical

        self._robot = placo.RobotWrapper(str(urdf_path), placo.Flags.ignore_collisions)
        self._limits = {n: tuple(math.degrees(v) for v in self._robot.get_joint_limits(n))
                        for n in self.joint_names}
        self._origin = self._pan_axis_origin()
        self._solver = self._make_solver(placo)
        self._pan_sign = self._measure_pan_sign()

    # ---- Kinematics protocol ------------------------------------------------------------
    def fk(self, joints: Sequence[float]) -> Vec3:
        self._set_arm(joints)
        x, y, z = self._tip_pose()[:3, 3] - self._origin
        return (float(x), float(y), float(z))

    def ik(self, target: Vec3, seed: Sequence[float]) -> Optional[List[float]]:
        target_arr = self._np.asarray(target, dtype=float) + self._origin  # robot frame -> URDF base_link
        self._pos_task.target_world = target_arr
        best = None
        for start in self._starts(target, seed):
            cand = self._solve(target_arr, start, seed)
            if cand is not None and (best is None or cand[:2] < best[:2]):
                best = cand
            if best is not None and best[0] < VERTICAL_DEG:
                break
        self.last_tilt_deg = None if best is None else best[0]
        if best is None or (self.max_tilt_deg is not None and best[0] > self.max_tilt_deg):
            return None
        return best[2] + [float(seed[5])]

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

    def _starts(self, target: Vec3, seed: Sequence[float]):
        yield [float(v) for v in seed[:5]]
        pan = self._pan_sign * math.degrees(math.atan2(target[1], target[0]))
        for lift, elbow, wrist in RESTART_POSTURES:
            yield [pan, lift, elbow, wrist, float(seed[4])]

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
        if err > MAX_POS_ERR or not self._within_limits(joints):
            return None
        tilt = math.degrees(math.acos(max(-1.0, min(1.0, -float(pose[2, 2])))))
        dist = max(abs(a - b) for a, b in zip(joints, seed[:5]))
        return (round(tilt, 3), dist, joints)

    def _iterate(self, target, reach_only: bool) -> None:
        """Solver steps until the tip is on target (reach_only) or the tilt is vertical / settled."""
        prev = None
        for _ in range(self.iterations):
            try:
                self._solver.solve(True)
            except RuntimeError:  # hard tip task infeasible from here: keep the last good state
                break
            self._robot.update_kinematics()
            down = float(self._tip_pose()[2, 2])
            if self._err(target) < 1e-4 and (reach_only or down < -0.99999
                                             or prev is not None and abs(prev - down) < 1e-8):
                break
            prev = down

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

    def _within_limits(self, joints: Sequence[float]) -> bool:
        return all(self._limits[n][0] - LIMIT_EPS_DEG <= v <= self._limits[n][1] + LIMIT_EPS_DEG
                   for n, v in zip(self.joint_names, joints))


def make_kinematics(cfg) -> PlacoKinematics:
    """Factory used by hal/real.py and hal/sim.py."""
    return PlacoKinematics(str(cfg.path(cfg.robot.urdf)), cfg.robot.ee_frame, cfg.robot.joint_names[:5])
