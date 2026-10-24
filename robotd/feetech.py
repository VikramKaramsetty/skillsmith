"""Feetech backend: robotd's `Bus` over LeRobot's SO101 follower. OWNER: WS1.

Runs only on the GX10, inside the LeRobot venv (`~/robot/.venv`, docs/RUNBOOK_GX10.md). LeRobot is
imported lazily, so this module imports anywhere and the tests use a fake follower.

Positions are LeRobot's calibrated units (`use_degrees=True`): degrees centred on each joint's
calibrated range, gripper 0..100. LeRobot's dicts are keyed by motor name; this module turns them
into lists in JOINT_NAMES order and back.

Connecting never moves the arm. LeRobot's own `connect()` re-enables torque with whatever goal the
servos last held (the arm jumps if it was moved by hand since), and calibrates interactively on a
mismatch. So robotd connects the bus, refuses a calibration mismatch, writes Goal = Present, and
only then runs LeRobot's `configure()` (torque off, modes, PID, gripper limits, torque on).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional, Sequence, Tuple

from robotd.controller import JOINT_NAMES

RETRIES = 2  # Feetech buses sometimes return a corrupt status packet; LeRobot retries at once
LOAD_SIGN_BIT = 1024  # STS3215 Present_Load: bit 10 is the direction, bits 0-9 the magnitude
STS3215_STEPS = 4096  # ticks per turn; every SO101 motor is an STS3215 (LeRobot: degrees = (ticks - mid) * 360 / 4095)

log = logging.getLogger("robotd")


class FeetechBus:
    """controller.Bus over a connected LeRobot SO follower (anything with its `.bus` API)."""

    def __init__(self, robot: Any):
        self._robot = robot
        self._bus = robot.bus

    def read_present(self) -> List[float]:
        present = self._bus.sync_read("Present_Position", num_retry=RETRIES)
        return [float(present[name]) for name in JOINT_NAMES]

    def read_gripper_load(self) -> float:
        """Magnitude of the gripper servo's load, in ~0.1 % of max torque."""
        load = abs(int(self._bus.read("Present_Load", "gripper", num_retry=RETRIES)))
        return float(load - LOAD_SIGN_BIT if load >= LOAD_SIGN_BIT else load)

    def write_goal(self, goal: Sequence[float]) -> None:
        self._bus.sync_write("Goal_Position", {name: float(g) for name, g in zip(JOINT_NAMES, goal)})

    def set_torque(self, on: bool) -> None:
        if on:
            self._bus.enable_torque(num_retry=RETRIES)
        else:
            self._bus.disable_torque(num_retry=RETRIES)

    def torque_enabled(self) -> bool:
        flags = self._bus.sync_read("Torque_Enable", num_retry=RETRIES)
        return all(bool(flags[name]) for name in JOINT_NAMES)

    def joint_limits(self) -> List[Tuple[float, float]]:
        """The calibrated ranges in LeRobot's units: degrees about each range's middle, gripper 0..100."""
        limits = []
        for name in JOINT_NAMES[:-1]:
            cal = self._robot.calibration[name]
            half = (cal.range_max - cal.range_min) / 2 * 360 / (STS3215_STEPS - 1)
            limits.append((-half, half))
        return limits + [(0.0, 100.0)]

    def close(self) -> None:
        log.warning("disconnecting the arm: torque goes off and the arm goes limp, support it")
        self._robot.disconnect()


def connect_follower(port: str, calibration_id: str,
                     make_robot: Optional[Callable[[str, str], Any]] = None) -> FeetechBus:
    """Open the follower without moving it. Raises on a calibration mismatch or a missing LeRobot."""
    robot = (make_robot or _lerobot_follower)(port, calibration_id)
    robot.bus.connect()
    try:
        if not robot.is_calibrated:
            raise RuntimeError(
                f"the motors' calibration does not match '{calibration_id}.json'; robotd never calibrates: "
                f"run lerobot-calibrate --robot.type=so101_follower --robot.port={port} --robot.id={calibration_id}")
        present = robot.bus.sync_read("Present_Position", num_retry=RETRIES)
        robot.bus.sync_write("Goal_Position", present)  # a stale goal can no longer make the arm jump
        robot.configure()  # LeRobot's setup; ends with torque on, holding `present`
    except Exception:
        robot.bus.disconnect(disable_torque=False)  # leave the torque as we found it
        raise
    log.info("follower on %s connected (calibration '%s'), holding its current pose", port, calibration_id)
    return FeetechBus(robot)


def _lerobot_follower(port: str, calibration_id: str) -> Any:
    try:
        from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig
    except ImportError as e:
        raise ImportError(
            "LeRobot is not installed in this Python: run robotd with ~/robot/.venv/bin/python "
            f"(docs/RUNBOOK_GX10.md), or use --mock ({e})") from e
    return SO101Follower(SO101FollowerConfig(port=port, id=calibration_id, use_degrees=True))
