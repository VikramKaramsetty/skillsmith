"""Live world model. OWNER: WS3c (WS3.5–WS3.7). Implements the Perception protocol for the real arm.

Contract (docs/ARCHITECTURE.md "Perception and world state"):
- Runs a background loop at 10–15 Hz (`world.rate_hz`): overhead frame → detector (YOLO-World/
  YOLO-E, HSV backup) → ray–plane localisation (camera calibration) → association to stable ids.
- `observe() -> Scene`: the LATEST state, not a fresh one-off detection. Objects hidden behind
  the arm keep their last position with `visible=False`.
- A held object follows the gripper (FK tip position from robotd) instead of being re-detected.
- `unexpected_motion() -> str | None`: a message (and clears it) when a NOT-held object moved
  more than `world.bump_threshold` (default 1 cm), e.g. "red cup moved 3.2 cm toward +y while
  not held". The Robot raises WorldChanged on it before the next motion.
- `perception_ok() -> (bool, str)`: the confidence gate (markers still where calibrated,
  detection confidence, object count stable). The classifier uses it. (WS3.6)
- `align_offset(obj) -> (dx, dy)`: wrist-camera fine correction in metres, used by robot.align_over. (WS3.7)
- `holding_check() -> (bool, str)`: wrist-camera post-grasp check. (WS3.7)
- `snapshot(path)`: save the latest frame for evidence.
- Reads frames ONLY through robotd (it owns the cameras).

Tracking rules (WS3.5):
- Detections are duck-typed `Detection`s (label, color, kind, point_uv, confidence). `point_uv` is
  the object's TOP centre; it is ray-cast at the object's known top height (sizes from the
  known-object table; `world.objects` overrides `KNOWN_OBJECTS`). An object sitting on another
  tracked object is re-cast at the stacked top height.
- Association is greedy nearest-neighbour per (color, kind) within `world.assoc_radius`. A new
  detection gets a stable id `<color>_<kind>_<n>`; ids are never reused.
- A track with no detection this frame is hidden: `visible=False`, last position kept, never "moved".
- When the arm reports holding, the track nearest the tip becomes held; it follows the FK tip with
  the offset it had at grasp time, and detections near it are absorbed. On release it re-anchors
  on its next detection (where it really landed is not a bump).
- A not-held track whose detections stay more than `world.bump_threshold` from its anchor for
  `world.bump_frames` consecutive frames fires ONE message, then re-anchors at the new position.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from skillsmith.events import EventBus, NullBus
from skillsmith.types import Scene, SceneObject, Vec3

# kind -> (height, width) in metres. Measure yours and override with `world.objects` in robot.yaml:
#   objects: {cup: {height: 0.08, width: 0.065}}
KNOWN_OBJECTS: Dict[str, Tuple[float, float]] = {
    "cup": (0.08, 0.065),
    "block": (0.03, 0.03),
    "ball": (0.04, 0.04),
}
DEFAULT_SIZE = (0.05, 0.05)
ASSOC_RADIUS = 0.08  # m; a detection farther than this from every same-kind track starts a new one
GRAB_RADIUS = 0.06  # m; the held object is the track nearest the tip, within this
BUMP_FRAMES = 2  # consecutive frames over the threshold before a bump fires (one bad frame is noise)
RATE_WINDOW = 30  # steps averaged by rate()


@dataclass
class _Track:
    obj: SceneObject
    anchor: Optional[Tuple[float, float]]  # rest position bumps are measured from; None = re-anchor
    over: int = 0  # consecutive frames beyond the bump threshold


class WorldModel:
    def __init__(self, cfg, frame_source=None, kinematics=None, arm=None, *, detector=None,
                 camera=None, bus: Optional[EventBus] = None,
                 decode: Optional[Callable[[Any], Any]] = None):
        world = cfg.get("world")
        self.rate_hz = float(_opt(world, "rate_hz", 12))
        self.bump_threshold = float(_opt(world, "bump_threshold", 0.01))
        self.min_confidence = float(_opt(world, "min_confidence", 0.5))
        self.assoc_radius = float(_opt(world, "assoc_radius", ASSOC_RADIUS))
        self.bump_frames = int(_opt(world, "bump_frames", BUMP_FRAMES))
        self.sizes = dict(KNOWN_OBJECTS)
        objects = _opt(world, "objects", None)
        for kind, size in (objects.raw() if objects is not None else {}).items():
            self.sizes[kind] = (float(size["height"]), float(size["width"]))

        self.frames = frame_source if frame_source is not None else _default_frames(cfg)
        self.detector = detector if detector is not None else _default_detector(cfg)
        self.camera = camera if camera is not None else _default_camera(cfg)
        self.kin = kinematics
        self.arm = arm
        self.bus = bus if bus is not None else NullBus()
        self._decode = decode if decode is not None else (lambda frame: frame.bgr())

        self._lock = threading.Lock()
        self._tracks: Dict[str, _Track] = {}
        self._counts: Dict[str, int] = {}
        self._held: Optional[str] = None
        self._held_offset: Vec3 = (0.0, 0.0, 0.0)
        self._bumps: List[str] = []
        self._frame = None
        self._scene: Optional[Scene] = None
        self._last_error: Optional[str] = None
        self._step_times: List[float] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---- Perception protocol ----------------------------------------------------------
    def observe(self) -> Scene:
        """The latest tracked state. Before the loop has produced one, runs a single step."""
        with self._lock:
            scene = self._scene
        if scene is None:
            self.step()
            with self._lock:
                scene = self._scene
        return scene if scene is not None else Scene((), time.time(), "camera:none")

    def unexpected_motion(self) -> Optional[str]:
        with self._lock:
            if not self._bumps:
                return None
            message = "; ".join(self._bumps)
            self._bumps.clear()
            return message

    def perception_ok(self) -> Tuple[bool, str]:
        raise NotImplementedError("WS3.6")

    def align_offset(self, obj: SceneObject) -> Tuple[float, float]:
        raise NotImplementedError("WS3.7")

    def holding_check(self) -> Tuple[bool, str]:
        raise NotImplementedError("WS3.7")

    def snapshot(self, path: str) -> Optional[str]:
        with self._lock:
            frame = self._frame
        if frame is None:
            return None
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(frame.jpeg)
        return str(out)

    # ---- the live loop ----------------------------------------------------------------
    def start(self) -> WorldModel:
        """Run `step()` in a background thread at `rate_hz` until `close()`."""
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="world-model", daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        close = getattr(self.frames, "close", None)
        if callable(close):
            close()

    def rate(self) -> float:
        """Achieved update rate in Hz over the recent steps (0 until two steps ran)."""
        with self._lock:
            times = list(self._step_times)
        if len(times) < 2 or times[-1] <= times[0]:
            return 0.0
        return (len(times) - 1) / (times[-1] - times[0])

    def _loop(self) -> None:
        period = 1.0 / self.rate_hz
        next_t = time.monotonic()
        while not self._stop.is_set():
            self.step()
            next_t += period
            delay = next_t - time.monotonic()
            if delay < 0:  # fell behind: carry on at the rate, no catch-up burst
                next_t = time.monotonic()
            self._stop.wait(max(0.0, delay))

    def step(self) -> bool:
        """One update: frame → detections → tracks → Scene. Returns False (and emits an error
        event once per distinct reason) when no update was possible; the last Scene is kept."""
        try:
            frame = self.frames.read()
            detections = list(self.detector.detect(self._decode(frame)))
            tip, holding = self._arm_state()
        except Exception as e:
            self._fail(f"{type(e).__name__}: {e}")
            return False
        with self._lock:
            self._frame = frame
            self._update_held(tip, holding)
            self._associate(self._localise(detections))
            self._scene = Scene(tuple(t.obj for t in self._tracks.values()), frame.t, _source(frame))
            self._step_times = (self._step_times + [time.monotonic()])[-RATE_WINDOW:]
            recovered = self._last_error is not None
            self._last_error = None
        if recovered:
            self.bus.emit("note", text="world model is updating again")
        return True

    def _fail(self, message: str) -> None:
        with self._lock:
            repeat = message == self._last_error
            self._last_error = message
        if not repeat:
            self.bus.emit("error", where="perception.world", message=message)

    def _arm_state(self) -> Tuple[Optional[Vec3], bool]:
        if self.arm is None or self.kin is None:
            return None, False
        tip = tuple(float(v) for v in self.kin.fk(self.arm.joints()))
        return tip, bool(self.arm.gripper_holding())

    # ---- held object --------------------------------------------------------------------
    def _update_held(self, tip: Optional[Vec3], holding: bool) -> None:
        if holding and tip is not None:
            if self._held is None:
                self._grab(tip)
            if self._held is not None:
                track = self._tracks[self._held]
                dx, dy, dz = self._held_offset
                track.obj = replace(track.obj, x=tip[0] + dx, y=tip[1] + dy, z=max(0.0, tip[2] + dz),
                                    visible=True)
        elif self._held is not None:
            track = self._tracks[self._held]
            track.anchor, track.over = None, 0  # where it really landed is not a bump
            self._held = None

    def _grab(self, tip: Vec3) -> None:
        near = [(math.hypot(t.obj.x - tip[0], t.obj.y - tip[1]), tid) for tid, t in self._tracks.items()]
        near = [(d, tid) for d, tid in near if d <= GRAB_RADIUS]
        if not near:
            return
        self._held = min(near)[1]
        obj = self._tracks[self._held].obj
        self._held_offset = (obj.x - tip[0], obj.y - tip[1], obj.z - tip[2])

    # ---- detections → positions ----------------------------------------------------------
    def _localise(self, detections: Sequence[Any]) -> List[SceneObject]:
        out = []
        for det in detections:
            confidence = float(getattr(det, "confidence", 1.0))
            if confidence < self.min_confidence:
                continue  # uncertain: the confidence gate (WS3.6) reports it, tracking ignores it
            height, width = self.sizes.get(det.kind, DEFAULT_SIZE)
            u, v = det.point_uv
            base = 0.0
            x, y = self.camera.pixel_to_table(u, v, base + height)
            below = self._support(det.kind, det.color, x, y, width)
            if below is not None:
                base = below.top
                x, y = self.camera.pixel_to_table(u, v, base + height)
            out.append(SceneObject(id="", label=det.label, color=det.color, kind=det.kind, x=x, y=y,
                                   z=base, height=height, width=width, confidence=confidence))
        return out

    def _support(self, kind: str, color: str, x: float, y: float, width: float) -> Optional[SceneObject]:
        """A tracked, not-held object of another colour/kind right under (x, y): we are stacked on it."""
        for tid, track in self._tracks.items():
            o = track.obj
            if tid == self._held or (o.color, o.kind) == (color, kind):
                continue
            if math.hypot(o.x - x, o.y - y) < (o.width + width) / 4:
                return o
        return None

    # ---- association and bumps -----------------------------------------------------------
    def _associate(self, seen: List[SceneObject]) -> None:
        pairs = []
        for i, obj in enumerate(seen):
            for tid, track in self._tracks.items():
                if (track.obj.color, track.obj.kind) != (obj.color, obj.kind):
                    continue
                d = math.hypot(track.obj.x - obj.x, track.obj.y - obj.y)
                if d <= self.assoc_radius:
                    pairs.append((d, i, tid))
        used: set = set()
        matched: set = set()
        for _d, i, tid in sorted(pairs):
            if i in used or tid in matched:
                continue
            used.add(i)
            matched.add(tid)
            if tid != self._held:  # the held object follows the tip; its detection is absorbed
                self._update_track(tid, seen[i])
        for i, obj in enumerate(seen):
            if i not in used:
                tid = self._new_id(obj)
                self._tracks[tid] = _Track(replace(obj, id=tid), anchor=(obj.x, obj.y))
                matched.add(tid)
        for tid, track in self._tracks.items():
            if tid not in matched and tid != self._held and track.obj.visible:
                track.obj = replace(track.obj, visible=False)
                track.over = 0

    def _update_track(self, tid: str, obj: SceneObject) -> None:
        track = self._tracks[tid]
        track.obj = replace(obj, id=tid, visible=True)
        if track.anchor is None:
            track.anchor = (obj.x, obj.y)
            return
        ax, ay = track.anchor
        if math.hypot(obj.x - ax, obj.y - ay) <= self.bump_threshold:
            track.over = 0
            return
        track.over += 1
        if track.over >= self.bump_frames:
            self._bumps.append(_bump_message(obj.label, obj.x - ax, obj.y - ay))
            track.anchor, track.over = (obj.x, obj.y), 0

    def _new_id(self, obj: SceneObject) -> str:
        key = f"{obj.color}_{obj.kind}"
        n = self._counts.get(key, 0)
        self._counts[key] = n + 1
        return f"{key}_{n}"


def _bump_message(label: str, dx: float, dy: float) -> str:
    """'red cup moved 3.2 cm toward +y while not held'."""
    dist = math.hypot(dx, dy)
    axis = ("+x" if dx > 0 else "-x") if abs(dx) >= abs(dy) else ("+y" if dy > 0 else "-y")
    return f"{label} moved {dist * 100:.1f} cm toward {axis} while not held"


def _opt(section, name: str, default):
    return section.get(name, default) if section is not None else default


def _source(frame) -> str:
    """'robotd:front' → 'camera:front' (the Scene.source convention); other sources pass through."""
    src = getattr(frame, "source", "camera")
    return "camera:" + src.split(":", 1)[1] if src.startswith("robotd:") else src


def _default_frames(cfg):
    from skillsmith.perception.frames import make_frame_source

    return make_frame_source(cfg, "front")


def _default_detector(cfg):
    from skillsmith.perception.color import HsvDetector

    return HsvDetector(cfg)


def _default_camera(cfg):
    from skillsmith.perception.geometry import CameraModel

    path = _opt(cfg.get("cameras"), "calibration", "config/camera.json")
    return CameraModel.load(str(cfg.path(path)))
