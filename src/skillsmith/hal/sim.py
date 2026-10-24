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
  (pinned at its grasp point every physics step; same idea as a weld). Opening detaches it and lets
  it fall onto its support. So the sim checks geometry, reach and collisions, never friction or slip.
  A held object keeps its grasp-time orientation, like the fake world: above z ~ 0.08 the IK has to
  tilt the gripper (wrist_flex limit, see kinematics.py), and that tilt is NOT passed on to the
  object. A rim grip placed at 0.155 m tilts the gripper ~30 deg, and a rigid real grasp would tilt
  the cup with it.
- Allowed contacts: object↔table, object↔object (stacks, neighbours), jaws↔held object, jaws↔an
  object while the gripper is closing on it or right after releasing it (until they first separate),
  and the held object resting on the table or another object with less than PLACE_TOL penetration
  (the fake world's 5 mm). Every other contact stops the arm, is appended to `safety_issues()` as a
  COLLISION SafetyIssue with names and heights, and raises CollisionError. Issues accumulate until
  `reset_scene()`.
- The kinematic tip is the grasp point, kinematics.TIP_OFFSET past the fixed jaw (the URDF's
  gripper_frame_link is the fixed jaw's tip). The sim models the jaws as they are, so a skill that
  lands the fixed jaw on an object gets a CollisionError.

Rendering (WS2.5):
- `build_world(..., camera=CameraPose)` adds a fixed MuJoCo camera "overhead" at the real overhead
  camera's pose and pinhole intrinsics, so sim frames line up pixel for pixel with real frames
  (expected-vs-actual). `overhead_camera(cfg)` reads it from `cameras.calibration`
  (config/camera.json, WS3.2, X_cam = R @ X_robot + t); with no calibration on this machine it falls
  back to DEFAULT_CAMERA (a near-overhead view, robot base at the bottom of the image). Lens
  distortion is not rendered, so expect a few pixels of drift near the image corners.
- `SimRecorder` renders while physics steps: an mp4 at VIDEO_FPS of sim time (`arm.last_video_path`)
  and a JPEG of the state at the end of every arm command (kept in memory). Each recording gets its
  own folder `<repair.runs_dir>/sim/<stamp>-<pid>-<n>/`, created lazily, so the dashboard's
  runs-dir-only `/video` route can serve it.
- `arm.last_video_path` finishes the mp4 of the attempt so far and returns its path (None if nothing
  moved or rendering is unavailable). The runtime reads it right after the skill ends, which is the
  attempt boundary the arm cannot see; motion after that starts a new recording.
- `arm.keyframes(trace, phases) -> {phase: path}` writes `<phase>.jpg` next to the mp4: the frame at
  the end of each phase's last primitive call, matched by wall-clock (`PrimitiveCall.t`). Raw renders;
  annotation (boxes, crosshair) is WS3's `annotate_keyframe`, which can take the same CameraPose.
- `snapshot(path)` saves the current overhead render.
- Rendering never blocks the gate: no GL context (CI needs MUJOCO_GL=egl) means `render_error` is set,
  a RuntimeWarning is raised, and video, key frames and snapshots return None / {}. `sim.video: false`
  in the config turns the mp4 off (key frames stay); `sim.video_fps` overrides VIDEO_FPS.
"""

from __future__ import annotations

import itertools
import math
import os
import time
import warnings
from dataclasses import dataclass, replace
from pathlib import Path
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


def build_world(scene: Optional[Scene], mjcf_path: str, camera: Optional[CameraPose] = None) -> SimWorld:
    """Vendored arm scene + a table at z = 0 + one free body per SceneObject (+ the overhead camera)."""
    import mujoco

    spec = mujoco.MjSpec.from_file(str(mjcf_path))
    origin = _robot_origin(spec)
    _add_table(spec, origin)
    if camera is not None:
        _add_camera(spec, camera, origin)
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


# ---- rendering (WS2.5) ------------------------------------------------------------------------
OVERHEAD_CAMERA = "overhead"
DEFAULT_CAMERA = {             # used when config/camera.json is absent (robot frame, metres)
    "eye": (0.12, 0.0, 0.75), "target": (0.18, 0.0, 0.0), "up": (1.0, 0.0, 0.0),
    "size": (640, 480), "vfov_deg": 60.0,
}
VIDEO_FPS = 10                 # frames per second of SIM time
VIDEO_MAX_WIDTH = 480          # px; video frames are strided down to at most this, key frames stay full size
JPEG_QUALITY = 85
_RECORDING = itertools.count(1)


@dataclass
class CameraPose:
    """Pinhole camera in the robot frame, OpenCV convention: X_cam = R @ X_robot + t, pixels = K @ X_cam."""

    K: List[List[float]]
    R: List[List[float]]
    t: List[float]
    size: Tuple[int, int]      # (width, height) px
    source: str = "default"    # "config/camera.json" or "default"

    @classmethod
    def from_model(cls, cam, source: str) -> CameraPose:
        """From a WS3 CameraModel (perception.geometry); its size defaults to twice the principal point."""
        size = cam.size or [round(2 * cam.K[0][2]), round(2 * cam.K[1][2])]
        return cls(K=[list(map(float, r)) for r in cam.K], R=[list(map(float, r)) for r in cam.R],
                   t=[float(v) for v in cam.t], size=(int(size[0]), int(size[1])), source=source)

    @classmethod
    def look_at(cls, eye: Vec3, target: Vec3, up: Vec3, size: Tuple[int, int], vfov_deg: float) -> CameraPose:
        """Camera at `eye` looking at `target`, with `up` (robot frame) pointing up in the image."""
        z = _unit(_sub(target, eye))
        up_in_plane = _sub(up, _scale(z, _dot(up, z)))
        y = _scale(_unit(up_in_plane), -1.0)        # image y points down
        x = _cross(y, z)
        rot = [list(x), list(y), list(z)]
        t = [-_dot(row, eye) for row in rot]
        w, h = size
        f = (h / 2) / math.tan(math.radians(vfov_deg) / 2)
        return cls(K=[[f, 0.0, w / 2], [0.0, f, h / 2], [0.0, 0.0, 1.0]], R=rot, t=t, size=(w, h))

    def center(self) -> Vec3:
        """Camera centre in the robot frame: -R^T t."""
        return tuple(-sum(self.R[r][c] * self.t[r] for r in range(3)) for c in range(3))

    def project(self, p: Vec3) -> Tuple[float, float]:
        """Robot-frame point -> pixel (u right, v down), no lens distortion."""
        xc, yc, zc = (_dot(self.R[r], p) + self.t[r] for r in range(3))
        return self.K[0][0] * xc / zc + self.K[0][2], self.K[1][1] * yc / zc + self.K[1][2]


def overhead_camera(cfg) -> CameraPose:
    """The real overhead camera from `cameras.calibration` (WS3.2), or DEFAULT_CAMERA when not calibrated."""
    from skillsmith.perception.geometry import CalibrationMissing, CameraModel

    cameras = cfg.get("cameras")
    rel = cameras.get("calibration") if cameras is not None else None
    if rel:
        try:
            return CameraPose.from_model(CameraModel.load(cfg.path(rel)), source=str(rel))
        except CalibrationMissing:
            pass
    d = DEFAULT_CAMERA
    return CameraPose.look_at(d["eye"], d["target"], d["up"], d["size"], d["vfov_deg"])


def _add_camera(spec, cam: CameraPose, origin: Vec3) -> None:
    """A fixed world camera matching `cam`: MuJoCo looks down its -z with y up, OpenCV down +z with y down."""
    import mujoco
    import numpy as np

    rot = np.array(cam.R, float)
    cam_to_world = np.column_stack([rot[0], -rot[1], -rot[2]])  # columns: the MuJoCo camera's x, y, z
    quat = np.zeros(4)
    mujoco.mju_mat2Quat(quat, cam_to_world.flatten())
    w, h = cam.size
    (fx, _, cx), (_, fy, cy), _ = cam.K
    c = spec.worldbody.add_camera(name=OVERHEAD_CAMERA, pos=list(_add(cam.center(), origin)), quat=list(quat))
    c.resolution = [w, h]
    c.sensor_size = [float(w), float(h)]       # one sensor unit per pixel, so focal and principal are in px
    c.focal_pixel = [float(fx), float(fy)]
    # MuJoCo shifts the frustum by the opposite of the principal point's offset from the image
    # centre (checked pixel for pixel in tests/ws2/test_sim_render.py).
    c.principal_pixel = [float(w / 2 - cx), float(h / 2 - cy)]
    vis = spec.visual.global_
    vis.offwidth, vis.offheight = max(vis.offwidth, w), max(vis.offheight, h)


class SimRecorder:
    """Offscreen renders of one SimWorld: the attempt mp4, per-command key frames and snapshots."""

    def __init__(self, world: SimWorld, out_root: Path, video: bool = True, fps: float = VIDEO_FPS):
        self.world = world
        self.out_root = Path(out_root)
        self.video = video
        self.fps = float(fps)
        self.render_error: Optional[str] = None
        self._renderer = None
        model = world.model
        if _camera_id(model, OVERHEAD_CAMERA) < 0:
            self._fail(f"no '{OVERHEAD_CAMERA}' camera in the sim model (build_world was given no camera)")
        else:
            w, h = (int(v) for v in model.cam_resolution[_camera_id(model, OVERHEAD_CAMERA)])
            try:
                import mujoco

                self._renderer = mujoco.Renderer(model, h, w)
            except Exception as e:  # no GL context: keep the gate running without frames
                self._fail(f"{type(e).__name__}: {e} (headless? set MUJOCO_GL=egl)")
        self._dir: Optional[Path] = None
        self._writer = None
        self._video_path: Optional[str] = None
        self._next_t = 0.0
        self._marks: List[Tuple[float, bytes]] = []
        self.last_dir: Optional[Path] = None
        self.last_video: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self._renderer is not None

    def frame(self, data):
        """RGB uint8 array (h, w, 3) of `data` from the overhead camera."""
        self._renderer.update_scene(data, camera=OVERHEAD_CAMERA)
        return self._renderer.render()

    def tick(self, data) -> None:
        """Called every physics step: add a video frame when VIDEO_FPS sim time has passed."""
        if not (self.ok and self.video) or data.time + 1e-9 < self._next_t:
            return
        self._next_t = data.time + 1.0 / self.fps
        if self._writer is None:
            self._open_writer()
        self._writer.append_data(_video_frame(self.frame(data)))

    def mark(self, data) -> None:
        """Called at the end of every arm command: keep a JPEG of this state with the wall-clock time."""
        if self.ok:
            self._marks.append((time.time(), _jpeg(self.frame(data))))

    def finish(self) -> Optional[str]:
        """Close the mp4 of the current recording; later frames go to a new recording folder."""
        if self._writer is not None:
            self._writer.close()
            self._writer = None
            self.last_video = self._video_path
        if self._dir is not None:
            self.last_dir, self._dir = self._dir, None
        return self.last_video

    def restart(self) -> None:
        """A new attempt (reset_scene): finish the old recording and forget its key frames."""
        self.finish()
        self.last_video, self.last_dir = None, None
        self._marks = []
        self._next_t = 0.0

    def keyframes(self, trace, phases) -> Dict[str, str]:
        """{phase: jpg}: the last command-end frame before the call after each phase's last call."""
        self.finish()
        if not self._marks or not phases:
            return {}
        if self.last_dir is None:  # video off: the key frames still get a recording folder
            self._recording_dir()
            self.finish()
        out_dir = self.last_dir
        out: Dict[str, str] = {}
        for p in phases:
            jpg = _frame_before(self._marks, _phase_end(trace, p.end_index))
            if jpg is not None:
                path = out_dir / f"{p.phase}.jpg"
                path.write_bytes(jpg)
                out[p.phase] = str(path)
        return out

    def snapshot(self, data, path: str) -> Optional[str]:
        if not self.ok:
            return None
        import imageio.v3 as iio

        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        rgb = self.frame(data)
        if out.suffix.lower() in (".jpg", ".jpeg"):
            out.write_bytes(_jpeg(rgb))
        else:
            iio.imwrite(out, rgb)  # format from the extension (.png for the evidence snapshot, etc.)
        return str(out)

    def close(self) -> None:
        self.finish()
        if self._renderer is not None:
            self._renderer.close()
            self._renderer = None

    def _fail(self, why: str) -> None:
        self.render_error = why
        warnings.warn(f"sim rendering disabled, no video or key frames: {why}", RuntimeWarning, stacklevel=3)

    def _recording_dir(self) -> Path:
        if self._dir is None:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self._dir = self.out_root / f"{stamp}-{os.getpid()}-{next(_RECORDING)}"
            self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    def _open_writer(self) -> None:
        import imageio.v2 as imageio

        path = self._recording_dir() / "attempt.mp4"
        self._writer = imageio.get_writer(str(path), fps=self.fps, codec="libx264", quality=6,
                                          macro_block_size=2, pixelformat="yuv420p")
        self._video_path = str(path)


def _phase_end(trace, end_index: int) -> float:
    """Wall-clock time a phase ended: the start of the first call after it (inf if it ran to the end)."""
    return min((c.t for c in trace if c.index > end_index), default=math.inf)


def _frame_before(marks: List[Tuple[float, bytes]], t: float) -> Optional[bytes]:
    jpg = None
    for mt, data in marks:
        if mt > t:
            break
        jpg = data
    return jpg


def _video_frame(rgb):
    """Strided down to VIDEO_MAX_WIDTH, cropped to even sides for yuv420p."""
    step = max(1, math.ceil(rgb.shape[1] / VIDEO_MAX_WIDTH))
    small = rgb[::step, ::step]
    h, w = small.shape[0] // 2 * 2, small.shape[1] // 2 * 2
    return small[:h, :w]


def _jpeg(rgb) -> bytes:
    import imageio.v3 as iio

    return iio.imwrite("<bytes>", rgb, extension=".jpg", quality=JPEG_QUALITY)


def _camera_id(model, name: str) -> int:
    import mujoco

    return mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, name)


def _add(a: Sequence[float], b: Sequence[float]) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a: Sequence[float], b: Sequence[float]) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale(a: Sequence[float], s: float) -> Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Sequence[float], b: Sequence[float]) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(a: Sequence[float]) -> Vec3:
    n = math.sqrt(_dot(a, a))
    if n < 1e-9:
        raise ValueError(f"camera direction {tuple(a)} has no length (eye == target, or up along the view)")
    return _scale(a, 1.0 / n)


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

    def __init__(self, world: SimWorld, cfg, home: Sequence[float], recorder: Optional[SimRecorder] = None):
        import mujoco

        self._mj = mujoco
        self._rec = recorder
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
        try:
            self._move_joints(target, speed_dps)
        finally:
            self._mark()

    def _move_joints(self, target: Sequence[float], speed_dps: float) -> None:
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
            self._mark()
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
        self._grip_pose: Tuple[object, object] = (None, None)  # held: pos in the gripper frame, world quat
        self._issues: List[SafetyIssue] = []
        if self._rec is not None:
            self._rec.restart()

    # ---- Perception ---------------------------------------------------------------------------
    def observe(self) -> Scene:
        return self.world.observe(self.data)

    def snapshot(self, path: str) -> Optional[str]:
        return self._rec.snapshot(self.data, path) if self._rec is not None else None

    # ---- rendering (WS2.5) --------------------------------------------------------------------
    @property
    def last_video_path(self) -> Optional[str]:
        """The mp4 of the attempt so far (finished on read), or None if nothing was rendered."""
        return self._rec.finish() if self._rec is not None else None

    @property
    def render_error(self) -> Optional[str]:
        return self._rec.render_error if self._rec is not None else "no recorder"

    def keyframes(self, trace, phases) -> Dict[str, str]:
        """{phase: jpg path} rendered from the overhead camera at the end of each PhaseCheck."""
        return self._rec.keyframes(trace, phases) if self._rec is not None else {}

    def close(self) -> None:
        if self._rec is not None:
            self._rec.close()

    def _mark(self) -> None:
        if self._rec is not None:
            self._rec.mark(self.data)

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
        if self._rec is not None:
            self._rec.tick(self.data)  # before the contact check, so a collision's frame is in the video
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
        inv, rel_p = np.zeros(4), np.zeros(3)
        mj.mju_negQuat(inv, d.body(JAW_BODIES[0]).xquat)
        mj.mju_rotVecQuat(rel_p, d.body(oid).xpos - d.body(JAW_BODIES[0]).xpos, inv)
        self._grip_pose = (rel_p, d.body(oid).xquat.copy())
        self._held, self._released = oid, None
        d.ctrl[self._grip_act] = d.qpos[self._grip_q]  # hold the jaws where they stalled, no squeeze

    def _pin(self, oid: str) -> None:
        """Carry the held object at its grasp point on the gripper, keeping its grasp-time orientation."""
        import numpy as np

        mj, d, m = self._mj, self.data, self.model
        rel_p, quat = self._grip_pose
        gpos, gq = d.body(JAW_BODIES[0]).xpos, d.body(JAW_BODIES[0]).xquat
        pos = np.zeros(3)
        mj.mju_rotVecQuat(pos, rel_p, gq)
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
    world = build_world(scene, mjcf, camera=overhead_camera(cfg))
    arm = SimArm(world, cfg, home, recorder=_make_recorder(cfg, world))
    return Env("sim", arm, kin, arm)


def _make_recorder(cfg, world: SimWorld) -> SimRecorder:
    """Recordings go under <repair.runs_dir>/sim; `sim.video` / `sim.video_fps` are optional config keys."""
    repair = cfg.get("repair")
    runs = cfg.path(repair.get("runs_dir", "runs") if repair is not None else "runs")
    sim = cfg.get("sim")
    video = bool(sim.get("video", True)) if sim is not None else True
    fps = float(sim.get("video_fps", VIDEO_FPS)) if sim is not None else VIDEO_FPS
    return SimRecorder(world, runs / "sim", video=video, fps=fps)
