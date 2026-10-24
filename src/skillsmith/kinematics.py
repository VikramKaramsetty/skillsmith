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
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from skillsmith.types import Vec3


class PlacoKinematics:
    def __init__(self, urdf_path: str, ee_frame: str = "gripper_frame_link",
                 joint_names: Optional[Sequence[str]] = None):
        raise NotImplementedError("WS2.2: see docs/workstreams/WS2_kinematics_sim.md")

    def fk(self, joints: Sequence[float]) -> Vec3:
        raise NotImplementedError("WS2.2")

    def ik(self, target: Vec3, seed: Sequence[float]) -> Optional[List[float]]:
        raise NotImplementedError("WS2.2")


def make_kinematics(cfg) -> PlacoKinematics:
    """Factory used by hal/real.py and hal/sim.py."""
    return PlacoKinematics(str(cfg.path(cfg.robot.urdf)), cfg.robot.ee_frame, cfg.robot.joint_names[:5])
