"""Pure-Python fake world: a Cartesian "arm" and point-ish objects.

Good enough to exercise the whole pipeline (skills, safety, verify, repair loop,
dashboard) with no hardware, no MuJoCo and no Droid. Joints are fake: the first three
"joints" are the tip x, y, z in metres, the last is the gripper 0..100.

Physics rules (deliberately simple and documented, so failures are explainable):
- Closing the gripper holds an object if the tip is inside its footprint and between its
  base and top, and it is narrower than MAX_GRIP_WIDTH.
- A held object moves with the tip. If it would overlap another object's volume:
  CollisionError.
- With a closed, empty gripper, the tip may not enter any object's volume.
- Released objects drop onto whatever is under their centre (or the table).
- rim_slip=True ("real" mode): an object gripped above RIM_FRACTION of its height slips
  out on the first horizontal move. The sim check env leaves this off, which models a
  real sim-to-real gap that only the camera verify catches.
"""

from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

from skillsmith.types import CollisionError, Scene, SceneObject, Vec3

MAX_GRIP_WIDTH = 0.09
RIM_FRACTION = 0.7


@dataclass
class _Obj:
    id: str
    label: str
    color: str
    kind: str
    x: float
    y: float
    z: float
    height: float
    width: float

    @property
    def top(self) -> float:
        return self.z + self.height

    def contains_xy(self, x: float, y: float, shrink: float = 1.0) -> bool:
        return math.hypot(self.x - x, self.y - y) < self.width / 2 * shrink

    def freeze(self) -> SceneObject:
        return SceneObject(self.id, self.label, self.color, self.kind,
                           round(self.x, 4), round(self.y, 4), round(self.z, 4),
                           self.height, self.width)


class FakeWorld:
    """Implements ArmBackend, Kinematics and Perception in one object."""

    name = "fake"

    def __init__(self, objects: List[_Obj], cfg, rim_slip: bool = False):
        self.cfg = cfg
        self.rim_slip = rim_slip
        self._initial = copy.deepcopy(objects)
        self.objects = objects
        home = cfg.robot.home_xyz
        self._q = [home[0], home[1], home[2], 0.0, 0.0, float(cfg.robot.gripper_open)]
        self._held: Optional[_Obj] = None
        self._held_offset = 0.0  # held.z - tip_z
        self._held_frac = 0.0  # where on the object it was gripped (0 base .. 1 top)

    # ---- construction ---------------------------------------------------------------
    @classmethod
    def demo(cls, cfg, rim_slip: bool = False) -> FakeWorld:
        return cls([
            _Obj("blue_cup_0", "blue cup", "blue", "cup", 0.22, -0.08, 0.0, 0.08, 0.065),
            _Obj("red_cup_0", "red cup", "red", "cup", 0.22, 0.08, 0.0, 0.08, 0.065),
            _Obj("green_block_0", "green block", "green", "block", 0.15, 0.0, 0.0, 0.03, 0.03),
        ], cfg, rim_slip=rim_slip)

    @classmethod
    def from_scene(cls, scene: Scene, cfg, rim_slip: bool = False) -> FakeWorld:
        return cls([_Obj(o.id, o.label, o.color, o.kind, o.x, o.y, o.z, o.height, o.width)
                    for o in scene.objects], cfg, rim_slip=rim_slip)

    def reset_scene(self) -> None:
        """Put every object back where it started and go home (a human does this on real)."""
        self.objects = copy.deepcopy(self._initial)
        self._held = None
        home = self.cfg.robot.home_xyz
        self._q = [home[0], home[1], home[2], 0.0, 0.0, float(self.cfg.robot.gripper_open)]

    # ---- Kinematics -----------------------------------------------------------------
    def fk(self, joints: Sequence[float]) -> Vec3:
        return (joints[0], joints[1], joints[2])

    def ik(self, target: Vec3, seed: Sequence[float]) -> Optional[List[float]]:
        ws = self.cfg.workspace
        r = math.hypot(target[0], target[1])
        if r > ws.max_reach + 0.01 or r < ws.min_reach - 0.01 or target[2] < -0.001:
            return None
        return [target[0], target[1], target[2], 0.0, 0.0, seed[5]]

    # ---- ArmBackend -----------------------------------------------------------------
    def joints(self) -> List[float]:
        return list(self._q)

    def move_joints(self, target: Sequence[float], speed_dps: float) -> None:
        x0, y0, _ = self._q[0], self._q[1], self._q[2]
        x, y, z = target[0], target[1], target[2]
        if self._held is not None:
            if self.rim_slip and self._held_frac > RIM_FRACTION and math.hypot(x - x0, y - y0) > 0.02:
                self._drop()  # slipped as soon as the arm accelerated sideways
            else:
                self._check_held_collision(x, y, z)
        if self._held is None and self._q[5] < 50:
            self._check_tip_collision(x, y, z)
        self._q[0:3] = [x, y, z]
        if self._held is not None:
            self._held.x, self._held.y, self._held.z = x, y, z + self._held_offset

    def set_gripper(self, pct: float) -> None:
        closing = pct < 50
        if closing and self._held is None:
            tx, ty, tz = self._q[0:3]
            for o in self.objects:
                if o.contains_xy(tx, ty) and o.z <= tz <= o.top and o.width <= MAX_GRIP_WIDTH:
                    self._held = o
                    self._held_offset = o.z - tz
                    self._held_frac = (tz - o.z) / o.height
                    break
        if not closing and self._held is not None:
            self._drop()
        self._q[5] = float(pct)

    def gripper_holding(self) -> bool:
        return self._held is not None

    def stop(self) -> None:
        pass

    # ---- Perception -----------------------------------------------------------------
    def observe(self) -> Scene:
        return Scene(tuple(o.freeze() for o in self.objects), time.time(), "fake")

    def snapshot(self, path: str) -> Optional[str]:
        return None

    # ---- internals ------------------------------------------------------------------
    def _others(self) -> List[_Obj]:
        return [o for o in self.objects if o is not self._held]

    def _check_held_collision(self, x: float, y: float, z: float) -> None:
        h = self._held
        base = z + self._held_offset
        for o in self._others():
            overlap = math.hypot(o.x - x, o.y - y) < (o.width + h.width) / 2 * 0.8
            if overlap and base < o.top - 0.005 and base + h.height > o.z:
                raise CollisionError(
                    f"held {h.label} hit {o.label}: its base would be at z={base:.3f} "
                    f"but {o.label} top is z={o.top:.3f}")

    def _check_tip_collision(self, x: float, y: float, z: float) -> None:
        for o in self.objects:
            if o.contains_xy(x, y) and o.z <= z < o.top - 0.005:
                raise CollisionError(
                    f"closed gripper pushed into {o.label} at z={z:.3f} (top z={o.top:.3f})")

    def _drop(self) -> None:
        h = self._held
        self._held = None
        support = 0.0
        for o in self._others():
            if o.contains_xy(h.x, h.y):
                support = max(support, o.top)
        h.z = support
