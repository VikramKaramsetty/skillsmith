"""robotd's arm controller: the last layer between HTTP callers and the servo bus. OWNER: WS1.

It owns one `Bus` (the mock today, the Feetech bus in WS1.2) and turns API calls into goal writes:
- `move_joints` moves the 5 arm joints from where they actually are, in steps of at most
  `max_step_deg`, paced by `speed_dps` (capped at `max_speed_dps`), then waits until every joint is
  within `settle_tol_deg` of the target, else 409 with how far each joint still is.
  The gripper value in the target is ignored: only `set_gripper` moves the jaws, so a seed copied
  before a grasp can never loosen the grip mid-carry.
- `stop` holds the arm where it is (Goal = Present) and makes a running motion raise 409.
  The gripper keeps its goal, so a held object stays gripped.
- One motion at a time: a second one gets 409 instead of queueing behind the first.
- Torque on writes Goal = Present first, so the arm does not jump to a stale goal.
Joint limits and the command watchdog arrive in WS1.3.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Protocol, Sequence

JOINT_NAMES = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")
ARM = 5  # indices 0..4 are the arm joints; index 5 is the gripper
POLL_S = 0.01
STILL_PCT = 0.5  # gripper moved less than this between polls = not moving
# MEASURE (WS1.7): replace with the thresholds from the gripper signal test on our real cups.
HOLD_MIN_GAP = 5.0  # jaws stopped at least this far (0..100) short of the commanded position
HOLD_MIN_LOAD = 200.0  # while the gripper servo pushes at least this hard (raw load)

log = logging.getLogger("robotd")


class Bus(Protocol):
    """The servo bus. Positions are 6 values in JOINT_NAMES order: calibrated degrees, gripper 0..100."""

    def read_present(self) -> List[float]: ...

    def read_gripper_load(self) -> float: ...

    def write_goal(self, goal: Sequence[float]) -> None: ...

    def set_torque(self, on: bool) -> None: ...

    def torque_enabled(self) -> bool: ...

    def close(self) -> None: ...


class RobotdError(Exception):
    """A refused or failed command. `status` is the HTTP status the server answers with."""

    def __init__(self, status: int, message: str, **details: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details

    def payload(self) -> dict:
        return {"ok": False, "error": self.message, **self.details}


class ArmController:
    def __init__(self, bus: Bus, max_step_deg: float = 10.0, max_speed_dps: float = 60.0,
                 settle_tol_deg: float = 1.5, settle_timeout_s: float = 2.0, gripper_timeout_s: float = 1.5):
        self.bus = bus
        self.max_step_deg = max_step_deg
        self.max_speed_dps = max_speed_dps
        self.settle_tol_deg = settle_tol_deg
        self.settle_timeout_s = settle_timeout_s
        self.gripper_timeout_s = gripper_timeout_s
        self._bus_lock = threading.RLock()  # the serial bus takes one transaction at a time
        self._motion_lock = threading.Lock()
        self._stop = threading.Event()
        self._goal = self._read()

    # ---- API ---------------------------------------------------------------------------
    def state(self) -> dict:
        with self._bus_lock:
            present = self.bus.read_present()
            load = self.bus.read_gripper_load()
            torque = self.bus.torque_enabled()
            holding = _holding(present[ARM], self._goal[ARM], load)
        return {"joints": present, "holding": holding, "gripper_pos": present[ARM],
                "gripper_load": load, "torque": torque, "t": time.time()}

    def holding(self) -> bool:
        return self.state()["holding"]

    def move_joints(self, target: Sequence[float], speed_dps: float) -> None:
        goal = _vector(target, len(JOINT_NAMES), "target")[:ARM]
        speed = min(_positive(speed_dps, "speed_dps"), self.max_speed_dps)
        with self._motion("move_joints"):
            start = self._read()[:ARM]
            biggest = max(abs(g - s) for g, s in zip(goal, start))
            steps = max(1, math.ceil(biggest / self.max_step_deg))
            for k in range(1, steps + 1):
                self._write(arm=[s + (g - s) * k / steps for s, g in zip(start, goal)])
                self._pause(biggest / steps / speed, "move_joints")
            self._settle(goal)

    def set_gripper(self, pct: float) -> bool:
        pct = _number(pct, "pct")
        if not 0 <= pct <= 100:
            raise RobotdError(400, f"pct must be 0..100 (0 = closed), got {pct}")
        with self._motion("gripper"):
            self._write(gripper=pct)
            self._wait_gripper()
        return self.holding()

    def stop(self) -> None:
        self._stop.set()
        self._halt()

    def set_torque(self, on: bool) -> None:
        if on:
            with self._bus_lock:
                self._goal = self._read()
                self.bus.write_goal(list(self._goal))
                self.bus.set_torque(True)
            return
        self._stop.set()
        with self._bus_lock:
            self.bus.set_torque(False)
        log.warning("torque off: the arm goes limp, support it")

    # ---- internals ---------------------------------------------------------------------
    @contextmanager
    def _motion(self, what: str) -> Iterator[None]:
        if not self._motion_lock.acquire(blocking=False):
            raise RobotdError(409, f"{what} refused: another motion is still running")
        try:
            with self._bus_lock:
                torque = self.bus.torque_enabled()
            if not torque:
                raise RobotdError(409, f'{what} refused: torque is off (POST /torque {{"on": true}})')
            self._stop.clear()
            yield
        finally:
            self._motion_lock.release()

    def _read(self) -> List[float]:
        with self._bus_lock:
            return [float(v) for v in self.bus.read_present()]

    def _write(self, arm: Optional[Sequence[float]] = None, gripper: Optional[float] = None) -> None:
        with self._bus_lock:
            if arm is not None:
                self._goal[:ARM] = [float(a) for a in arm]
            if gripper is not None:
                self._goal[ARM] = float(gripper)
            self.bus.write_goal(list(self._goal))

    def _halt(self) -> None:
        with self._bus_lock:
            self._write(arm=self._read()[:ARM])

    def _pause(self, seconds: float, what: str) -> None:
        """Sleep, but wake at once on stop: hold position and fail the running command."""
        if self._stop.wait(seconds):
            self._halt()
            raise RobotdError(409, f"{what} stopped", joints=self._read())

    def _settle(self, goal: Sequence[float]) -> None:
        deadline = time.monotonic() + self.settle_timeout_s
        while True:
            errors = self._errors(goal)
            if not errors:
                return
            if time.monotonic() >= deadline:
                raise RobotdError(409, "move_joints did not reach the target", errors=errors,
                                  tolerance_deg=self.settle_tol_deg, joints=self._read())
            self._pause(POLL_S, "move_joints")

    def _errors(self, goal: Sequence[float]) -> Dict[str, float]:
        """Joints still further than settle_tol_deg from the target: name -> target - present (deg)."""
        present = self._read()
        return {JOINT_NAMES[i]: round(g - present[i], 2)
                for i, g in enumerate(goal) if abs(g - present[i]) > self.settle_tol_deg}

    def _wait_gripper(self) -> None:
        """Until the jaws reach the goal or stop moving (closed on an object), up to gripper_timeout_s."""
        deadline = time.monotonic() + self.gripper_timeout_s
        last: Optional[float] = None
        still = 0
        while time.monotonic() < deadline:
            pos = self._read()[ARM]
            if abs(pos - self._goal[ARM]) <= self.settle_tol_deg:
                return
            still = still + 1 if last is not None and abs(pos - last) < STILL_PCT else 0
            if still >= 3:
                return
            last = pos
            self._pause(POLL_S, "gripper")


def _holding(pos: float, goal: float, load: float) -> bool:
    return pos - goal >= HOLD_MIN_GAP and abs(load) >= HOLD_MIN_LOAD


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RobotdError(400, f"{name} must be a finite number, got {value!r}")
    return float(value)


def _positive(value: Any, name: str) -> float:
    number = _number(value, name)
    if number <= 0:
        raise RobotdError(400, f"{name} must be > 0, got {number}")
    return number


def _vector(value: Any, n: int, name: str) -> List[float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != n:
        raise RobotdError(400, f"{name} must be {n} numbers ({', '.join(JOINT_NAMES)}), got {value!r}")
    return [_number(v, f"{name}[{i}]") for i, v in enumerate(value)]
