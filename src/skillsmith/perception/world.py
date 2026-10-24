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

Confidence gate (WS3.6). `perception_ok()` is (False, reason) when any of these hold since the
baseline (the first update after construction or `reset_gate()`); issues are joined by "; " and
each starts with a fixed prefix the classifier can match:
- "world model stale: ..."  the latest update failed (camera gone, detector crashed).
- "marker drift X cm ..."   `camera.marker_drift(frame)` (run every `world.drift_every` updates;
  falls back to the median of `camera.marker_errors(frame)` while marker_drift is a stub)
  exceeded `world.max_marker_drift` (default 1 cm) on `DRIFT_CHECKS` checks in a row → CAMERA_MOVED.
- "low confidence: ..."     a detection under `world.min_confidence` for `LOW_CONF_FRAMES`
  frames in a row, away from the tip and the held object (the arm covering a cup lowers its
  score; that is not doubt about the table). Latched until `reset_gate()` → PERCEPTION_UNCERTAIN.
- "object count changed ..." a track not in the baseline seen for `NEW_FRAMES` frames, or a
  baseline object unseen for `world.lost_s` seconds of updates (not held) → PERCEPTION_UNCERTAIN.
The runtime should call `reset_gate()` when an attempt starts so the gate covers one attempt.
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
MAX_MARKER_DRIFT = 0.01  # m; calibration markers farther than this from where calibrated = camera moved
DRIFT_CHECKS = 2  # consecutive drift checks over the limit before the gate fails
LOW_CONF_FRAMES = 3  # consecutive frames with an uncertain detection before the gate fails
NEW_FRAMES = 3  # frames a new track must be seen before it counts as an extra object
LOST_S = 5.0  # s of updates a baseline object may stay unseen before it counts as missing
ARM_RADIUS = 0.08  # m; uncertain detections this close to the tip are the arm's occlusion, not doubt


@dataclass
class _Track:
    obj: SceneObject
    anchor: Optional[Tuple[float, float]]  # rest position bumps are measured from; None = re-anchor
    over: int = 0  # consecutive frames beyond the bump threshold
    seen: int = 1  # frames with a detection
    missed: int = 0  # consecutive frames without one


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
        self.max_marker_drift = float(_opt(world, "max_marker_drift", MAX_MARKER_DRIFT))
        self.drift_every = max(1, int(_opt(world, "drift_every", round(self.rate_hz))))
        self.lost_frames = max(1, round(float(_opt(world, "lost_s", LOST_S)) * self.rate_hz))
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
        self._steps = 0
        self._baseline: Optional[set] = None  # track ids at the gate's baseline; None = take it next update
        self._drift: Optional[float] = None  # last measured marker drift (m)
        self._drift_over = 0  # consecutive drift checks over the limit
        self._drift_unavailable: Optional[str] = None
        self._low_conf_run = 0
        self._low_conf: Optional[str] = None  # latched low-confidence issue
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
        """The confidence gate: (True, summary) or (False, "; "-joined issues). See module doc."""
        with self._lock:
            if self._scene is None:
                return False, "world model stale: no update yet" + (
                    f" ({self._last_error})" if self._last_error else "")
            issues = []
            if self._last_error is not None:
                issues.append(f"world model stale: {self._last_error}")
            if self._drift is not None and self._drift_over >= DRIFT_CHECKS:
                issues.append(f"marker drift {self._drift * 100:.1f} cm "
                              f"(limit {self.max_marker_drift * 100:.1f} cm): camera moved, recalibrate")
            if self._low_conf is not None:
                issues.append(self._low_conf)
            count = self._count_issue()
            if count is not None:
                issues.append(count)
            if issues:
                return False, "; ".join(issues)
            return True, self._ok_summary()

    def reset_gate(self) -> None:
        """Start a new gate window (call when an attempt starts): the next update is the baseline
        for the object count, and latched low-confidence issues clear. Marker drift is a property
        of the camera, so it stays until the markers are back in place."""
        with self._lock:
            self._baseline = None
            self._low_conf, self._low_conf_run = None, 0

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
            image = self._decode(frame)
            detections = list(self.detector.detect(image))
            tip, holding = self._arm_state()
        except Exception as e:
            self._fail(f"{type(e).__name__}: {e}")
            return False
        drift = self._measure_drift(image) if self._steps % self.drift_every == 0 else None
        with self._lock:
            self._steps += 1
            self._frame = frame
            self._update_held(tip, holding)
            self._associate(self._localise(detections))
            self._update_gate(detections, tip, drift)
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
            if tid in matched or tid == self._held:
                track.missed = 0
                continue
            track.missed += 1
            if track.obj.visible:
                track.obj = replace(track.obj, visible=False)
                track.over = 0

    def _update_track(self, tid: str, obj: SceneObject) -> None:
        track = self._tracks[tid]
        track.obj = replace(obj, id=tid, visible=True)
        track.seen += 1
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

    # ---- confidence gate (WS3.6) ---------------------------------------------------------
    def _measure_drift(self, image: Any) -> Optional[float]:
        """Marker drift in metres, or None when it could not be measured this time (markers
        hidden by the arm, or the camera model has no marker check: emitted once)."""
        try:
            drift = self._camera_drift(image)
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            if reason != self._drift_unavailable:
                self._drift_unavailable = reason
                self.bus.emit("error", where="perception.world",
                              message=f"marker drift not checked (confidence gate): {reason}")
            return None
        self._drift_unavailable = None
        return drift if math.isfinite(drift) else None

    def _camera_drift(self, image: Any) -> float:
        """`camera.marker_drift(image)`; while that is still a stub, the median of
        `camera.marker_errors(image)` (geometry's real calibration check), NaN when no marker is seen."""
        fn = getattr(self.camera, "marker_drift", None)
        try:
            if fn is None:
                raise NotImplementedError(f"{type(self.camera).__name__} has no marker_drift()")
            return float(fn(image))
        except NotImplementedError:
            errors_fn = getattr(self.camera, "marker_errors", None)
            if errors_fn is None:
                raise
        errors = sorted(float(e) for e in errors_fn(image).values())
        if not errors:
            return math.nan
        mid = len(errors) // 2
        return errors[mid] if len(errors) % 2 else (errors[mid - 1] + errors[mid]) / 2

    def _update_gate(self, detections: Sequence[Any], tip: Optional[Vec3], drift: Optional[float]) -> None:
        if drift is not None:
            self._drift = drift
            self._drift_over = self._drift_over + 1 if drift > self.max_marker_drift else 0
        if self._baseline is None:
            self._baseline = set(self._tracks)
        doubtful = self._uncertain(detections, tip)
        self._low_conf_run = self._low_conf_run + 1 if doubtful else 0
        if doubtful and self._low_conf_run >= LOW_CONF_FRAMES and self._low_conf is None:
            label, confidence = doubtful
            self._low_conf = (f"low confidence: {label} at {confidence:.2f} (< {self.min_confidence:.2f}) "
                              f"for {self._low_conf_run} frames")

    def _uncertain(self, detections: Sequence[Any], tip: Optional[Vec3]) -> Optional[Tuple[str, float]]:
        """The least confident detection under `min_confidence` that the arm does not explain."""
        worst: Optional[Tuple[str, float]] = None
        arm = [(tip[0], tip[1])] if tip is not None else []
        if self._held is not None:
            held = self._tracks[self._held].obj
            arm.append((held.x, held.y))
        for det in detections:
            confidence = float(getattr(det, "confidence", 1.0))
            if confidence >= self.min_confidence:
                continue
            height, _width = self.sizes.get(det.kind, DEFAULT_SIZE)
            x, y = self.camera.pixel_to_table(det.point_uv[0], det.point_uv[1], height)
            if any(math.hypot(x - px, y - py) <= ARM_RADIUS for px, py in arm):
                continue
            if worst is None or confidence < worst[1]:
                worst = (det.label, confidence)
        return worst

    def _count_issue(self) -> Optional[str]:
        baseline = self._baseline or set()
        new = [t.obj.label for tid, t in self._tracks.items() if tid not in baseline and t.seen >= NEW_FRAMES]
        lost = [self._tracks[tid].obj.label for tid in sorted(baseline)
                if tid != self._held and self._tracks[tid].missed >= self.lost_frames]
        if not new and not lost:
            return None
        parts = [f"{label} appeared" for label in new]
        parts += [f"{label} unseen for {self.lost_frames / self.rate_hz:.1f} s" for label in lost]
        now = len(baseline) + len(new) - len(lost)
        return f"object count changed {len(baseline)} → {now}: " + ", ".join(parts)

    def _ok_summary(self) -> str:
        drift = "markers unchecked" if self._drift is None else f"markers {self._drift * 100:.1f} cm"
        return f"perception ok: {drift}, {len(self._baseline or ())} objects stable, confidence fine"


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
