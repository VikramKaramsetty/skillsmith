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

Sim Env (WS2.4):
- `make_sim_env(cfg, scene)` builds the world (the fake demo table when `scene` is None, like
  `make_env("fake")`) and returns `Env("sim", arm, kin, arm)`: `SimArm` is the ArmBackend and the
  Perception, `kin` is the same `PlacoKinematics` the real arm uses. The URDF and the MJCF agree on
  every joint frame, so joint degrees mean the same thing in both. The arm starts at `home_xyz`.
- `move_joints` drives the position actuators along a straight joint-space line at `speed_dps`, then
  waits up to SETTLE_S for the joints to land within SETTLE_DEG, else raises SkillError.
- Grasping: closing the gripper with both jaws touching one object attaches it to the gripper
  (pinned at its grasp pose every physics step; same idea as a weld). Opening detaches it and lets
  it fall onto its support. So the sim checks geometry, reach and collisions, never friction or slip.
- Allowed contacts: object↔table, object↔object (stacks, neighbours), jaws↔held object, jaws↔an
  object while the gripper is closing on it or right after releasing it (until they first separate),
  and the held object resting on the table or another object with less than PLACE_TOL penetration
  (the fake world's 5 mm). Every other contact stops the arm, is appended to `safety_issues()` as a
  COLLISION SafetyIssue with names and heights, and raises CollisionError. Issues accumulate until
  `reset_scene()`.
- The kinematic tip (URDF gripper_frame_link) is the FIXED jaw's tip, about 2 cm from the centre
  between the jaws (MJCF site "gripperframe"). The sim keeps that geometry honest: a skill that puts
  the tip on an object's centre drives the fixed jaw into it and gets a CollisionError.
- `snapshot()` returns None and there is no `last_video_path` until WS2.5 renders frames and video
  (the runtime reads it with getattr, and QA's video test skips while it is absent).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Sequence, Tuple

from skillsmith.types import CollisionError, SafetyIssue, Scene, SceneObject, SkillError, Vec3

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


# ---- the sim Env (WS2.4) ----------------------------------------------------------------------
FLOOR_NAME = "floor"
JAW_BODIES = ("gripper", "moving_jaw_so101_v1")   # fixed jaw (on the gripper body), moving jaw
GRIPPER_JOINT = "gripper"
PLACE_TOL = 0.005      # m; held object may sink this far into its support (fake world: 5 mm)
SETTLE_DEG = 1.0       # arm joints count as arrived within this
SETTLE_S = 1.0         # s; extra time allowed after the trajectory before move_joints gives up
GRIPPER_S = 0.6        # s; time the jaws get to open or close
DROP_S = 0.4           # s; time a released object gets to land
HOME_SEED = (0.0, 0.0, 0.0, 90.0, 0.0)


class SimArm:
    """MuJoCo ArmBackend + Perception over one SimWorld. Joints in degrees, gripper 0..100."""

    name = "sim"

    def __init__(self, world: SimWorld, cfg, home: Sequence[float]):
        import mujoco

        self._mj = mujoco
        self.world = world
        self.cfg = cfg
        self.model = world.model
        self._home = [float(v) for v in home[:5]]
        m = self.model
        self._arm_joints = list(cfg.robot.joint_names[:5])
        self._qadr = [int(m.joint(n).qposadr[0]) for n in self._arm_joints]
        self._dadr = [int(m.joint(n).dofadr[0]) for n in self._arm_joints]
        self._act = [int(m.actuator(n).id) for n in self._arm_joints]
        self._grip_q = int(m.joint(GRIPPER_JOINT).qposadr[0])
        self._grip_act = int(m.actuator(GRIPPER_JOINT).id)
        self._grip_range = tuple(float(v) for v in m.joint(GRIPPER_JOINT).range)
        self._body_of = {int(m.body(oid).id): oid for oid in world.objects}
        self._jaw_ids = [int(m.body(b).id) for b in JAW_BODIES]
        self._ground = {int(m.geom(TABLE_NAME).id)}
        if _has_geom(m, FLOOR_NAME):
            self._ground.add(int(m.geom(FLOOR_NAME).id))
        self._free_ok = self._free_contact_table()
        self.reset_scene()

    # ---- ArmBackend ---------------------------------------------------------------------------
    def joints(self) -> List[float]:
        q = [math.degrees(float(self.data.qpos[a])) for a in self._qadr]
        lo, hi = self._grip_range
        return q + [round(100.0 * (float(self.data.qpos[self._grip_q]) - lo) / (hi - lo), 2)]

    def move_joints(self, target: Sequence[float], speed_dps: float) -> None:
        goal = [float(v) for v in target[:5]]
        self._check_limits(goal)
        start = self.joints()[:5]
        span = max(abs(g - s) for g, s in zip(goal, start))
        steps = max(1, math.ceil(span / max(float(speed_dps), 1e-6) / self.model.opt.timestep))
        for k in range(1, steps + 1):
            self._set_arm_ctrl([s + (g - s) * k / steps for s, g in zip(start, goal)])
            self._step()
        for _ in range(self._n(SETTLE_S)):
            if self._arrived(goal):
                return
            self._step()
        if not self._arrived(goal):
            worst = max(range(5), key=lambda i: abs(self.joints()[i] - goal[i]))
            raise SkillError(f"sim arm did not reach its target: {self._arm_joints[worst]} is "
                             f"{abs(self.joints()[worst] - goal[worst]):.1f} deg off after {SETTLE_S} s")

    def set_gripper(self, pct: float) -> None:
        closing = pct < 50
        lo, hi = self._grip_range
        if not closing and self._held is not None:
            self._released, self._held = self._held, None
        self.data.ctrl[self._grip_act] = lo + (hi - lo) * min(max(float(pct), 0.0), 100.0) / 100.0
        self._gripping = closing
        try:
            for _ in range(self._n(GRIPPER_S if closing else GRIPPER_S + DROP_S)):
                self._step()
        finally:
            self._gripping = False
        if closing and self._held is None:
            self._attach(self._pinched())

    def gripper_holding(self) -> bool:
        return self._held is not None

    def stop(self) -> None:
        self._set_arm_ctrl(self.joints()[:5])

    def safety_issues(self) -> List[SafetyIssue]:
        return list(self._issues)

    def reset_scene(self) -> None:
        """Objects back at their Scene poses, arm at home, gripper open, issues cleared."""
        self.data = self.world.make_data()
        for adr, deg in zip(self._qadr, self._home):
            self.data.qpos[adr] = math.radians(deg)
        self.data.qpos[self._grip_q] = self._grip_range[1]
        self._set_arm_ctrl(self._home)
        self.data.ctrl[self._grip_act] = self._grip_range[1]
        self._mj.mj_forward(self.model, self.data)
        self._held: Optional[str] = None
        self._released: Optional[str] = None
        self._gripping = False
        self._grip_pose: Tuple[object, object] = (None, None)  # held object's pos, quat in the gripper frame
        self._issues: List[SafetyIssue] = []

    # ---- Perception ---------------------------------------------------------------------------
    def observe(self) -> Scene:
        return self.world.observe(self.data)

    def snapshot(self, path: str) -> Optional[str]:
        return None

    # ---- physics ------------------------------------------------------------------------------
    def _n(self, seconds: float) -> int:
        return max(1, int(round(seconds / self.model.opt.timestep)))

    def _set_arm_ctrl(self, deg: Sequence[float]) -> None:
        for act, v in zip(self._act, deg):
            self.data.ctrl[act] = math.radians(v)

    def _step(self) -> None:
        self._mj.mj_step(self.model, self.data)
        if self._held is not None:
            self._pin(self._held)
        self._check_contacts()

    def _arrived(self, goal: Sequence[float]) -> bool:
        q = self.joints()
        slow = all(abs(float(self.data.qvel[d])) < math.radians(5.0) for d in self._dadr)
        return slow and all(abs(a - b) < SETTLE_DEG for a, b in zip(q, goal))

    def _check_limits(self, goal: Sequence[float]) -> None:
        for name, v in zip(self._arm_joints, goal):
            lo, hi = (math.degrees(float(r)) for r in self.model.joint(name).range)
            if not lo - 0.1 <= v <= hi + 0.1:
                raise SkillError(f"sim: {name} target {v:.1f} deg is outside its limits [{lo:.1f}, {hi:.1f}]")

    # ---- grasping -----------------------------------------------------------------------------
    def _pinched(self) -> Optional[str]:
        """The object both jaws touch, if any."""
        touching: Dict[str, set] = {}
        for g1, g2, _ in self._contacts():
            for a, b in ((g1, g2), (g2, g1)):
                oid = self._object_of(b)
                jaw = int(self.model.geom_bodyid[a])
                if oid is not None and jaw in self._jaw_ids:
                    touching.setdefault(oid, set()).add(jaw)
        return next((oid for oid, jaws in touching.items() if len(jaws) == len(self._jaw_ids)), None)

    def _attach(self, oid: Optional[str]) -> None:
        if oid is None:
            return
        import numpy as np

        mj, d = self._mj, self.data
        gq, oq = d.body(JAW_BODIES[0]).xquat, d.body(oid).xquat
        inv, rel_q, rel_p = np.zeros(4), np.zeros(4), np.zeros(3)
        mj.mju_negQuat(inv, gq)
        mj.mju_mulQuat(rel_q, inv, oq)
        mj.mju_rotVecQuat(rel_p, d.body(oid).xpos - d.body(JAW_BODIES[0]).xpos, inv)
        self._grip_pose = (rel_p, rel_q)
        self._held, self._released = oid, None
        d.ctrl[self._grip_act] = d.qpos[self._grip_q]  # hold the jaws where they stalled, no squeeze

    def _pin(self, oid: str) -> None:
        """Move the held object rigidly with the gripper body (its grasp-time relative pose)."""
        import numpy as np

        mj, d, m = self._mj, self.data, self.model
        rel_p, rel_q = self._grip_pose
        gpos, gq = d.body(JAW_BODIES[0]).xpos, d.body(JAW_BODIES[0]).xquat
        pos, quat = np.zeros(3), np.zeros(4)
        mj.mju_rotVecQuat(pos, rel_p, gq)
        mj.mju_mulQuat(quat, gq, rel_q)
        jnt = int(m.body(oid).jntadr[0])
        qa, da = int(m.jnt_qposadr[jnt]), int(m.jnt_dofadr[jnt])
        d.qpos[qa:qa + 3] = gpos + pos
        d.qpos[qa + 3:qa + 7] = quat
        d.qvel[da:da + 6] = 0.0

    # ---- contacts -----------------------------------------------------------------------------
    def _free_contact_table(self):
        """ok[g1, g2]: contacts that are always fine while nothing is held (table/objects among themselves)."""
        import numpy as np

        m = self.model
        passive = [g for g in range(m.ngeom) if g in self._ground or self._object_of(g) is not None]
        ok = np.zeros((m.ngeom, m.ngeom), dtype=bool)
        ok[np.ix_(passive, passive)] = True
        return ok

    def _contacts(self):
        n = self.data.ncon
        if n == 0:
            return []
        c = self.data.contact
        return list(zip(c.geom1[:n].tolist(), c.geom2[:n].tolist(), c.dist[:n].tolist()))

    def _check_contacts(self) -> None:
        n = self.data.ncon
        if n == 0:
            return
        c = self.data.contact
        g1, g2 = c.geom1[:n], c.geom2[:n]
        suspect = ~self._free_ok[g1, g2]
        if self._held is not None:
            held = int(self.model.body(self._held).id)
            suspect |= (self.model.geom_bodyid[g1] == held) | (self.model.geom_bodyid[g2] == held)
        idx = suspect.nonzero()[0]
        if len(idx) == 0:
            return
        if self._released is not None and not any(self._is_released_jaw(int(g1[i]), int(g2[i])) for i in idx):
            self._released = None  # the jaws let go of it; from now on touching it is a collision
        for i in idx:
            a, b, dist = int(g1[i]), int(g2[i]), float(c.dist[i])
            if not self._contact_ok(a, b, dist):
                self._collide(a, b, dist, [float(v) for v in c.pos[i]])

    def _contact_ok(self, a: int, b: int, dist: float) -> bool:
        oa, ob = self._object_of(a), self._object_of(b)
        held = self._held
        if held is not None and held in (oa, ob):
            other_obj, other_geom = (ob, b) if oa == held else (oa, a)
            if other_obj is not None or other_geom in self._ground:
                return dist > -PLACE_TOL          # resting on its support, not pushed into it
            return int(self.model.geom_bodyid[other_geom]) in self._jaw_ids
        if self._gripping and self._jaw_and_object(a, b) is not None:
            return True
        if self._released is not None and self._is_released_jaw(a, b):
            return True
        return bool(self._free_ok[a, b])

    def _jaw_and_object(self, a: int, b: int) -> Optional[str]:
        for jaw, obj in ((a, b), (b, a)):
            if int(self.model.geom_bodyid[jaw]) in self._jaw_ids and self._object_of(obj) is not None:
                return self._object_of(obj)
        return None

    def _is_released_jaw(self, a: int, b: int) -> bool:
        return self._jaw_and_object(a, b) == self._released

    def _collide(self, a: int, b: int, dist: float, pos: List[float]) -> None:
        if self._object_of(a) is not None and self._object_of(a) != self._held:
            a, b = b, a  # the mover first: "held blue cup hit red cup", "gripper hit green block"
        z = _mm(self.world.to_robot(tuple(pos))[2])
        msg = f"{self._describe(a)} hit {self._describe(b)} at z={z:.3f} m"
        if self._held is not None and self._held in (self._object_of(a), self._object_of(b)) and dist < 0:
            msg += f", {-dist * 1000:.1f} mm deep (limit {PLACE_TOL * 1000:.0f} mm)"
        for g in (a, b):
            oid = self._object_of(g)
            if oid is not None and oid != self._held:
                base, top = self._span(oid)
                msg += f"; {self.world.objects[oid].label} spans z={_mm(base):.3f}..{_mm(top):.3f}"
        self.stop()
        self._issues.append(SafetyIssue("COLLISION", f"sim contact: {msg}"))
        raise CollisionError(msg)

    def _describe(self, g: int) -> str:
        oid = self._object_of(g)
        if oid is not None:
            label = self.world.objects[oid].label
            return f"held {label}" if oid == self._held else label
        if g in self._ground:
            return "the table"
        name = self._mj.mj_id2name(self.model, self._mj.mjtObj.mjOBJ_GEOM, g)
        return name or self._mj.mj_id2name(self.model, self._mj.mjtObj.mjOBJ_BODY, int(self.model.geom_bodyid[g]))

    def _object_of(self, g: int) -> Optional[str]:
        return self._body_of.get(int(self.model.geom_bodyid[g]))

    def _span(self, oid: str) -> Tuple[float, float]:
        obj = self.world.objects[oid]
        base = self.world.to_robot(tuple(float(v) for v in self.data.body(oid).xpos))[2] - _half_height(obj)
        return base, base + obj.height


def _mm(v: float) -> float:
    """Round to the millimetre without printing -0.000."""
    return round(v, 3) + 0.0


def _has_geom(model, name: str) -> bool:
    import mujoco

    return mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, name) >= 0


def make_sim_env(cfg, scene: Optional[Scene] = None):
    """Env("sim") built from `scene` (default: the fake demo table), arm at home, placo kinematics."""
    from skillsmith.hal.base import Env
    from skillsmith.kinematics import make_kinematics

    if scene is None:
        from skillsmith.hal.fake import FakeWorld

        scene = FakeWorld.demo(cfg).observe()
    mjcf = cfg.path(cfg.robot.mjcf)
    if not mjcf.is_file():
        raise NotImplementedError(f"SO101 MJCF not vendored at {mjcf}: run python assets/so101/fetch_so101.py")
    kin = make_kinematics(cfg)
    hx, hy, hz = cfg.robot.home_xyz
    home = kin.ik((hx, hy, hz), [*HOME_SEED, float(cfg.robot.gripper_open)])
    if home is None:
        raise ValueError(f"robot.home_xyz {cfg.robot.home_xyz} is unreachable")
    arm = SimArm(build_world(scene, mjcf), cfg, home)
    return Env("sim", arm, kin, arm)
