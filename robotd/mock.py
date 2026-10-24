"""Mock hardware for `python -m robotd --mock` and the WS1 tests. OWNER: WS1.

Behaves like the Feetech bus seen through LeRobot: positions in calibrated degrees (gripper
0..100), servos drive to their goal only while torque is on, and the gripper jaws stop on an
object (position stalls short of the goal, load rises).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

FRAME_JPEG = Path(__file__).with_name("mock_frame.jpg")
FREE_LOAD = 30.0  # raw gripper servo load moving freely (Feetech units, ~0.1 % of max torque)
GRIP_LOAD = 450.0  # raw gripper servo load squeezing an object
# roughly an SO101's calibrated ranges (deg; wrist_roll turns fully; gripper 0..100)
MOCK_LIMITS = ((-110.0, 110.0), (-100.0, 100.0), (-100.0, 100.0), (-100.0, 100.0), (-180.0, 180.0), (0.0, 100.0))


class MockBus:
    """Six servos that reach their goal at once while torque is on. Implements controller.Bus."""

    def __init__(self, joints: Sequence[float] = (0, 0, 0, 0, 0), gripper: float = 100.0,
                 object_at: Optional[float] = None, torque: bool = True,
                 limits: Sequence[Tuple[float, float]] = MOCK_LIMITS):
        self.present: List[float] = [float(j) for j in joints] + [float(gripper)]
        self.goal: List[float] = list(self.present)
        self.torque = torque
        self.object_at = object_at  # gripper position where the jaws meet an object; None = nothing there
        self.limits = [(float(lo), float(hi)) for lo, hi in limits]
        self.log: List[Tuple[str, object]] = []  # ("goal", [6 values]) and ("torque", bool), in order

    def read_present(self) -> List[float]:
        return list(self.present)

    def read_gripper_load(self) -> float:
        return GRIP_LOAD if self._squeezing() else FREE_LOAD

    def write_goal(self, goal: Sequence[float]) -> None:
        self.goal = [float(g) for g in goal]
        self.log.append(("goal", list(self.goal)))
        if self.torque:
            self._drive()

    def set_torque(self, on: bool) -> None:
        self.torque = on
        self.log.append(("torque", on))
        if on:
            self._drive()

    def torque_enabled(self) -> bool:
        return self.torque

    def joint_limits(self) -> List[Tuple[float, float]]:
        return list(self.limits)

    def close(self) -> None:
        pass

    def _drive(self) -> None:
        self.present = list(self.goal)
        if self.object_at is not None:
            self.present[5] = max(self.goal[5], self.object_at)

    def _squeezing(self) -> bool:
        return self.torque and self.object_at is not None and self.goal[5] < self.object_at


class MockCamera:
    """Serves the same placeholder JPEG on every read."""

    def __init__(self, name: str = "mock"):
        self.name = name
        self._jpeg = FRAME_JPEG.read_bytes()

    def latest(self) -> Tuple[bytes, float]:
        return self._jpeg, time.time()
