"""Hardware abstraction contracts. See docs/INTERFACES.md §2.

fake, sim and real backends all implement ArmBackend + Kinematics + Perception, so a
skill runs unchanged on each.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Protocol, Sequence

from skillsmith.types import Scene, Vec3

if TYPE_CHECKING:
    from skillsmith.config import Config


class ArmBackend(Protocol):
    name: str

    def joints(self) -> List[float]:
        """6 values: 5 arm joints in degrees, gripper 0..100 last."""

    def move_joints(self, target: Sequence[float], speed_dps: float) -> None:
        """Blocking. Returns when within tolerance, raises on failure or collision."""

    def set_gripper(self, pct: float) -> None:
        """Blocking. 0 = closed, 100 = open."""

    def gripper_holding(self) -> bool: ...

    def stop(self) -> None: ...


class Kinematics(Protocol):
    def fk(self, joints: Sequence[float]) -> Vec3:
        """Gripper tip position for a joint vector."""

    def ik(self, target: Vec3, seed: Sequence[float]) -> Optional[List[float]]:
        """Joints that put the tip at target pointing down, or None if unreachable.
        The gripper value is copied from seed."""


class Perception(Protocol):
    def observe(self) -> Scene: ...

    def snapshot(self, path: str) -> Optional[str]:
        """Save a camera frame to path; return the path or None if no camera."""


@dataclass
class Env:
    name: str
    arm: ArmBackend
    kin: Kinematics
    perception: Perception

    def close(self) -> None:
        for part in (self.arm, self.kin, self.perception):
            close = getattr(part, "close", None)
            if callable(close):
                close()


def make_env(name: str, cfg: Config, scene: Optional[Scene] = None, bus=None) -> Env:
    """Build an Env. 'fake' and 'sim' take an optional Scene to build the world from; 'real' passes `bus`
    to its WorldModel so bumps, drift and camera errors reach the run's events and the dashboard."""
    if name == "fake":
        from skillsmith.hal.fake import FakeWorld

        world = FakeWorld.from_scene(scene, cfg) if scene else FakeWorld.demo(cfg)
        return Env("fake", world, world, world)
    if name == "sim":
        from skillsmith.hal.sim import make_sim_env

        return make_sim_env(cfg, scene)
    if name == "real":
        from skillsmith.hal.real import make_real_env

        return make_real_env(cfg, bus)
    raise ValueError(f"unknown env '{name}' (expected fake | sim | real)")
