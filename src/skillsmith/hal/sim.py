"""MuJoCo sim backend. OWNER: WS2 (docs/workstreams/WS2_kinematics_sim.md).

Contract (docs/INTERFACES.md §2):
- make_sim_env(cfg, scene) -> Env with arm/kin/perception backed by one MuJoCo model.
- The world is built FROM the latest real Scene: each SceneObject becomes a body
  (cylinder for cups, box for blocks) at its x, y, z with its height and width.
- arm.move_joints steps physics until the joints settle (or times out → raise).
- Unintended contacts (arm/held object vs table or other objects, excluding the
  gripper pads against the grasped object) raise CollisionError AND are exposed via
  arm.safety_issues() -> list[SafetyIssue].
- arm.last_video_path: an mp4 of the attempt rendered offscreen (for the dashboard).
- perception.observe() returns ground-truth object poses from the sim.
- Kinematics should be the SAME implementation used on the real arm (placo via
  LeRobot RobotKinematics on so101_new_calib.urdf) so sim validates the real IK.

Scene builder (WS2.3):
- `build_world(scene, mjcf_path)` loads the vendored Menagerie scene, adds a table whose top is
  the plane z = 0 and one free body per SceneObject, and returns a `SimWorld`.
- Robot frame vs MuJoCo world: the MJCF base sits at the world origin, so the robot frame origin
  (on the table under the shoulder_pan axis) is at the pan joint's anchor with z = 0. It is read
  from the compiled model, never hard-coded. `SimWorld.to_world` / `to_robot` convert.
- Shapes: cup = solid cylinder (diameter = width), block = box (edge = width), ball = sphere
  (diameter = width), anything else = box. Cups are solid on purpose: a stacked object rests on
  the cup's top, which is what `verify.on_top` and the fake world assume. The sim checks
  geometry, reach and collisions, not friction, slip or a cup's hollow inside.
- Body and geom names are the SceneObject ids, so contacts read "blue_cup_0 vs table".
- `SimWorld.observe(data)` reads the ground-truth poses back into a Scene (source "sim").
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Dict, Optional, Tuple

from skillsmith.types import Scene, SceneObject, Vec3

TABLE_NAME = "table"
TABLE_HALF = (0.30, 0.40)      # m; table half extents in x, y (robot frame), centred at TABLE_CENTER
TABLE_CENTER = (0.15, 0.0)     # m; robot frame: the workspace sits in front of the base
TABLE_THICKNESS = 0.03         # m; the table top is z = 0, the vendored floor plane moves under it
OBJECT_MASS = {"cup": 0.03, "block": 0.02, "ball": 0.02}  # kg; light, like the real props
DEFAULT_MASS = 0.03
COLORS = {
    "red": (0.85, 0.15, 0.15), "blue": (0.15, 0.35, 0.85), "green": (0.15, 0.65, 0.25),
    "yellow": (0.95, 0.85, 0.15), "orange": (0.95, 0.55, 0.10), "purple": (0.55, 0.25, 0.70),
    "white": (0.92, 0.92, 0.92), "black": (0.10, 0.10, 0.10), "pink": (0.95, 0.55, 0.70),
}
UNKNOWN_COLOR = (0.55, 0.55, 0.55)


@dataclass
class SimWorld:
    """A compiled MuJoCo model of the arm, the table and the Scene's objects."""

    model: object                       # mujoco.MjModel
    origin: Vec3                        # robot frame origin in MuJoCo world coordinates
    objects: Dict[str, SceneObject]     # id -> the SceneObject each body was built from

    def to_world(self, p: Vec3) -> Vec3:
        return (p[0] + self.origin[0], p[1] + self.origin[1], p[2] + self.origin[2])

    def to_robot(self, p: Vec3) -> Vec3:
        return (p[0] - self.origin[0], p[1] - self.origin[1], p[2] - self.origin[2])

    def make_data(self):
        """Fresh MjData with forward kinematics computed (objects at their Scene poses)."""
        import mujoco

        data = mujoco.MjData(self.model)
        mujoco.mj_forward(self.model, data)
        return data

    def observe(self, data, t: Optional[float] = None) -> Scene:
        """Ground-truth Scene: each object's base centre (robot frame) from the sim state."""
        objs = []
        for oid, obj in self.objects.items():
            cx, cy, cz = self.to_robot(tuple(float(v) for v in data.body(oid).xpos))
            objs.append(replace(obj, x=round(cx, 4), y=round(cy, 4), z=round(cz - _half_height(obj), 4),
                                confidence=1.0, visible=True))
        return Scene(objects=tuple(objs), t=time.time() if t is None else t, source="sim")


def build_world(scene: Optional[Scene], mjcf_path: str) -> SimWorld:
    """Vendored arm scene + a table at z = 0 + one free body per SceneObject."""
    import mujoco

    spec = mujoco.MjSpec.from_file(str(mjcf_path))
    origin = _robot_origin(spec)
    _add_table(spec, origin)
    objects: Dict[str, SceneObject] = {}
    for obj in (scene.objects if scene else ()):
        if obj.id in objects:
            raise ValueError(f"duplicate SceneObject id {obj.id!r}")
        _add_object(spec, obj, origin)
        objects[obj.id] = obj
    return SimWorld(model=spec.compile(), origin=origin, objects=objects)


def _robot_origin(spec) -> Vec3:
    """Robot frame origin in world coordinates: the shoulder_pan anchor dropped to the table."""
    import mujoco

    model = spec.compile()
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    x, y, _ = (float(v) for v in data.joint("shoulder_pan").xanchor)
    return (x, y, 0.0)


def _add_table(spec, origin: Vec3) -> None:
    import mujoco

    floor = spec.geom("floor")
    if floor is not None:  # keep the vendored floor for looks, just under the table
        floor.pos = [0.0, 0.0, -TABLE_THICKNESS]
    cx, cy = TABLE_CENTER[0] + origin[0], TABLE_CENTER[1] + origin[1]
    spec.worldbody.add_geom(name=TABLE_NAME, type=mujoco.mjtGeom.mjGEOM_BOX,
                            size=[TABLE_HALF[0], TABLE_HALF[1], TABLE_THICKNESS / 2],
                            pos=[cx, cy, -TABLE_THICKNESS / 2], rgba=[0.75, 0.65, 0.50, 1.0])


def _add_object(spec, obj: SceneObject, origin: Vec3) -> None:
    import mujoco

    if obj.height <= 0 or obj.width <= 0:
        raise ValueError(f"{obj.id}: height and width must be > 0 (got {obj.height}, {obj.width})")
    gtype, size = _shape(obj)
    body = spec.worldbody.add_body(name=obj.id, pos=list(_center_world(obj, origin)))
    body.add_freejoint()
    body.add_geom(name=obj.id, type=getattr(mujoco.mjtGeom, gtype), size=size,
                  rgba=[*COLORS.get(obj.color.lower(), UNKNOWN_COLOR), 1.0],
                  mass=OBJECT_MASS.get(obj.kind, DEFAULT_MASS))


def _shape(obj: SceneObject) -> Tuple[str, list]:
    """MuJoCo geom type and size for a SceneObject (sizes are half extents / radii)."""
    r, h = obj.width / 2, obj.height / 2
    if obj.kind == "cup":
        return "mjGEOM_CYLINDER", [r, h]
    if obj.kind == "ball":
        return "mjGEOM_SPHERE", [r]
    return "mjGEOM_BOX", [r, r, h]


def _center_world(obj: SceneObject, origin: Vec3) -> Vec3:
    """Body centre in world coordinates: SceneObject x, y, z are its base centre in the robot frame."""
    return (obj.x + origin[0], obj.y + origin[1], obj.z + _half_height(obj) + origin[2])


def _half_height(obj: SceneObject) -> float:
    """Base-to-centre distance of the built geom (a sphere's comes from its width)."""
    return obj.width / 2 if obj.kind == "ball" else obj.height / 2


def make_sim_env(cfg, scene: Optional[Scene] = None):
    raise NotImplementedError(
        "sim backend not built yet (WS2.4; the scene builder is build_world). "
        "Use --env fake, or see docs/workstreams/WS2_kinematics_sim.md")
