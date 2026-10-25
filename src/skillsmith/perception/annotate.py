"""Annotated key frames for Droid and the dashboard. OWNER: WS3b (WS3.8).

Contract:
- `annotate_keyframe(frame_bgr, scene, camera, overlays) -> frame_bgr`, where overlays may hold:
  boxes + labels for every object, a crosshair at the planned target, a dot at the gripper tip
  (from FK, projected with the calibrated camera), and an error arrow labelled in cm.
- `crop_pair(before, after, scene, names) -> (img, img)`: tight crops around the objects named
  in the success predicate (for Droid and the optional frontier-vision second opinion).
- Draw with OpenCV, imported lazily. Text big enough to read at 640×480.

Everything is drawn in pixels. `camera` is anything with `project(x, y, z) -> (u, v)` (the
calibrated `CameraModel`, or a stub in tests); it turns robot-frame points (scene objects, the
planned target, the FK tip) into pixels. Points already in pixels (`*_uv`, detection boxes) are
drawn as given, so a caller without a calibration can still annotate. The input frame is never
modified; a copy is returned.

Geometry decides pass/fail; these images only explain it to Droid and the human.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Tuple, Union

UV = Tuple[float, float]
XYZ = Tuple[float, float, float]
BBox = Tuple[int, int, int, int]  # x0, y0, x1, y1 (pixels, inclusive)

# BGR colours for boxes, by the object's colour word; anything else is drawn white.
BOX_BGR = {
    "red": (40, 40, 230),
    "orange": (0, 140, 255),
    "yellow": (0, 220, 240),
    "green": (60, 200, 60),
    "blue": (230, 120, 30),
    "purple": (200, 60, 160),
}
TARGET_BGR = (0, 255, 255)  # yellow crosshair: where the skill planned to go
GRIPPER_BGR = (255, 0, 255)  # magenta dot: where the gripper tip actually is (FK)
ERROR_BGR = (0, 0, 255)  # red arrow: planned target -> actual position
TEXT_BG = (20, 20, 20)


@dataclass
class Overlays:
    """What to draw on one key frame. Robot-frame points need a camera; `*_uv` points do not.

    `actual` is where the thing that should be at `target` really ended up (usually the moved
    object's top centre); the error arrow runs target -> actual and is labelled with `error_cm`,
    or with their horizontal distance when both are robot-frame points."""

    detections: Sequence[Any] = ()  # Detection-like: label, bbox, optional color/confidence
    target: Optional[XYZ] = None
    target_uv: Optional[UV] = None
    gripper: Optional[XYZ] = None
    gripper_uv: Optional[UV] = None
    actual: Optional[XYZ] = None
    actual_uv: Optional[UV] = None
    error_cm: Optional[float] = None
    title: str = ""  # e.g. "carry · SLIP_IN_CARRY"
    scene_boxes: bool = True  # box every scene object (needs a camera); detections win per label

    @classmethod
    def of(cls, value: Union[Overlays, dict, None]) -> Overlays:
        """An `Overlays` from itself, a plain dict with the same keys, or None (nothing extra)."""
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        known = {f.name for f in fields(cls)}
        unknown = set(value) - known
        if unknown:
            raise ValueError(f"unknown overlay keys {sorted(unknown)}; expected some of {sorted(known)}")
        return cls(**value)


@dataclass(frozen=True)
class Box:
    label: str
    bbox: BBox
    color: str = ""
    dashed: bool = False  # a hidden object (last seen position), drawn dashed
    confidence: Optional[float] = None


@dataclass
class Plan:
    """Everything `annotate_keyframe` will draw, resolved to pixels (pure Python, testable)."""

    boxes: list[Box] = field(default_factory=list)
    target: Optional[UV] = None
    gripper: Optional[UV] = None
    arrow: Optional[Tuple[UV, UV, str]] = None  # start, end, label
    title: str = ""


# ---------------------------------------------------------------- pure geometry


def project(camera: Any, point: Optional[Sequence[float]]) -> Optional[UV]:
    if point is None or camera is None:
        return None
    u, v = camera.project(float(point[0]), float(point[1]), float(point[2]))
    return (float(u), float(v))


def object_bbox(camera: Any, obj: Any) -> BBox:
    """Pixel box around a scene object: the projection of its base and top squares."""
    r = obj.width / 2.0
    corners = [
        (obj.x + dx, obj.y + dy, z)
        for z in (obj.z, obj.z + obj.height)
        for dx in (-r, r)
        for dy in (-r, r)
    ]
    uvs = [camera.project(*c) for c in corners]
    us = [round(p[0], 6) for p in uvs]  # rounded so float noise never adds a pixel
    vs = [round(p[1], 6) for p in uvs]
    return (math.floor(min(us)), math.floor(min(vs)), math.ceil(max(us)), math.ceil(max(vs)))


def _det_color(det: Any) -> str:
    color = getattr(det, "color", "")
    if color:
        return color
    words = str(getattr(det, "label", "")).split()
    return words[0] if len(words) > 1 else ""


def plan_overlays(scene: Any, camera: Any, overlays: Union[Overlays, dict, None]) -> Plan:
    """Resolve scene objects, detections and overlay points to pixels."""
    ov = Overlays.of(overlays)
    plan = Plan(title=ov.title)
    for det in ov.detections:
        conf = getattr(det, "confidence", None)
        plan.boxes.append(Box(str(det.label), tuple(int(c) for c in det.bbox), _det_color(det), False, conf))
    detected = {b.label for b in plan.boxes}
    if ov.scene_boxes and camera is not None and scene is not None:
        for obj in scene.objects:
            if obj.label in detected:
                continue
            label = obj.label if obj.visible else f"{obj.label} (hidden)"
            plan.boxes.append(Box(label, object_bbox(camera, obj), obj.color, not obj.visible))
    plan.target = ov.target_uv or project(camera, ov.target)
    plan.gripper = ov.gripper_uv or project(camera, ov.gripper)
    actual = ov.actual_uv or project(camera, ov.actual)
    if plan.target is not None and actual is not None:
        cm = ov.error_cm
        if cm is None and ov.target is not None and ov.actual is not None:
            cm = 100.0 * math.hypot(ov.actual[0] - ov.target[0], ov.actual[1] - ov.target[1])
        plan.arrow = (plan.target, actual, f"{cm:.1f} cm" if cm is not None else "error")
    return plan


def _named_objects(scenes: Iterable[Any], names: Sequence[str]) -> list[Any]:
    return [obj for scene in scenes for name in names if (obj := scene.find(name)) is not None]


def crop_window(bboxes: Sequence[BBox], size: Tuple[int, int], pad: float = 0.2, min_side: int = 96) -> BBox:
    """One square window (inclusive pixels) around the union of `bboxes`, padded by `pad` of its
    larger side on each edge, at least `min_side` wide, kept inside an image of `size` (w, h)."""
    if not bboxes:
        raise ValueError("crop_window needs at least one box")
    w, h = size
    x0 = min(b[0] for b in bboxes)
    y0 = min(b[1] for b in bboxes)
    x1 = max(b[2] for b in bboxes)
    y1 = max(b[3] for b in bboxes)
    side = max(x1 - x0 + 1, y1 - y0 + 1)
    side = min(max(int(round(side * (1.0 + 2.0 * pad))), min_side), w, h)
    left = math.floor((x0 + x1) / 2.0 - side / 2.0 + 0.5)
    top = math.floor((y0 + y1) / 2.0 - side / 2.0 + 0.5)
    left = min(max(left, 0), w - side)
    top = min(max(top, 0), h - side)
    return (left, top, left + side - 1, top + side - 1)


# ---------------------------------------------------------------- drawing (OpenCV, lazy)


def annotate_keyframe(frame_bgr, scene, camera, overlays=None):
    """A copy of `frame_bgr` with object boxes and labels, the planned-target crosshair, the
    gripper-tip dot and the target -> actual error arrow labelled in cm."""
    plan = plan_overlays(scene, camera, overlays)
    img = frame_bgr.copy()
    style = _Style.for_width(img.shape[1])
    for box in plan.boxes:
        _draw_box(img, box, style)
    if plan.arrow is not None:
        _draw_arrow(img, plan.arrow, style)
    if plan.target is not None:
        _draw_crosshair(img, plan.target, style)
    if plan.gripper is not None:
        _draw_gripper(img, plan.gripper, style)
    if plan.title:
        _text(img, plan.title, (style.pad, style.pad), (255, 255, 255), style, anchor="top")
    return img


def crop_pair(before_bgr, after_bgr, scene, names, camera=None, *, boxes=None, pad=0.2, min_side=96):
    """The same window cut from `before_bgr` and `after_bgr`, tight around the objects in `names`
    (ids or labels from the success predicate) so the two crops compare like for like.

    Object pixels come from `boxes` ({name: bbox}, e.g. from detections) and/or from projecting
    the named objects of `scene` with `camera`. `scene` may be one Scene or several (before and
    after), so an object that moved stays inside the window. Raises ValueError when none of the
    names can be located: a crop of the wrong place would mislead Droid."""
    pixel_boxes = [tuple(int(c) for c in boxes[n]) for n in names if boxes and n in boxes]
    if camera is not None and scene is not None:
        scenes = scene if isinstance(scene, (list, tuple)) else [scene]
        pixel_boxes += [object_bbox(camera, o) for o in _named_objects(scenes, names)]
    if not pixel_boxes:
        raise ValueError(f"crop_pair: none of {list(names)} could be located (need boxes, or scene + camera)")
    h, w = before_bgr.shape[:2]
    if after_bgr.shape[:2] != (h, w):
        raise ValueError(f"crop_pair: before is {w}x{h} but after is {after_bgr.shape[1]}x{after_bgr.shape[0]}")
    x0, y0, x1, y1 = crop_window(pixel_boxes, (w, h), pad, min_side)
    return before_bgr[y0 : y1 + 1, x0 : x1 + 1].copy(), after_bgr[y0 : y1 + 1, x0 : x1 + 1].copy()


def side_by_side(before_bgr, after_bgr, labels: Tuple[str, str] = ("before", "after"), height: int = 240):
    """Two crops scaled to one `height`, side by side with captions: one image for the dashboard
    drawer or a vision prompt."""
    import cv2
    import numpy as np

    style = _Style.for_width(640)
    tiles = []
    for img, label in zip((before_bgr, after_bgr), labels):
        width = max(1, int(round(img.shape[1] * height / img.shape[0])))
        tile = cv2.resize(img, (width, height), interpolation=cv2.INTER_LINEAR)
        _text(tile, label, (4, 4), (255, 255, 255), style, anchor="top")
        tiles.append(tile)
    gap = np.full((height, 4, 3), 255, dtype=tiles[0].dtype)
    return np.hstack([tiles[0], gap, tiles[1]])


def caption_strip(img, lines: Sequence[str], width_chars: int = 110):
    """`img` with a white strip underneath holding `lines` (wrapped), dark text: the readable part of a
    key frame for Droid (phase, verdict, class, what ran, the classifier's summary and hint)."""
    import textwrap

    import cv2
    import numpy as np

    wrapped = [w for line in lines if line for w in textwrap.wrap(str(line), width_chars)]
    scale = max(0.45, img.shape[1] / 1800.0)
    line_h = int(28 * scale / 0.6)
    strip = np.full((line_h * len(wrapped) + line_h // 2, img.shape[1], 3), 255, dtype=img.dtype)
    for k, text in enumerate(wrapped):
        bold = 2 if k == 0 else 1
        cv2.putText(strip, text, (10, line_h * (k + 1)), cv2.FONT_HERSHEY_SIMPLEX, scale, (20, 20, 20), bold,
                    cv2.LINE_AA)
    return np.vstack([img, strip])


def write_image(path: Union[str, Path], img) -> str:
    """Save `img` (creating parent folders) and return the path, for `AttemptResult.keyframes`."""
    import cv2

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), img):
        raise OSError(f"could not write image {path}")
    return str(path)


@dataclass(frozen=True)
class _Style:
    font_scale: float
    thickness: int
    pad: int
    marker: int

    @classmethod
    def for_width(cls, width: int) -> _Style:
        k = max(width, 320) / 640.0  # 640x480 is the reference size
        return cls(0.6 * k, max(1, round(2 * k)), max(4, round(6 * k)), max(8, round(14 * k)))


def _pt(uv: UV) -> Tuple[int, int]:
    return (int(round(uv[0])), int(round(uv[1])))


def _text(img, text: str, org: Tuple[int, int], color, style: _Style, anchor: str = "bottom") -> None:
    """Text on a dark box so it reads on any background. `org` is the box's top-left corner
    (anchor="top") or bottom-left corner (anchor="bottom"); the box is kept on screen."""
    import cv2

    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw, th), base = cv2.getTextSize(text, font, style.font_scale, style.thickness)
    h, w = img.shape[:2]
    bw, bh = tw + style.pad, th + base + style.pad
    x = min(max(org[0], 0), max(w - bw, 0))
    y = org[1] if anchor == "top" else org[1] - bh
    y = min(max(y, 0), max(h - bh, 0))
    cv2.rectangle(img, (x, y), (x + bw, y + bh), TEXT_BG, -1)
    org = (x + style.pad // 2, y + th + style.pad // 2)
    cv2.putText(img, text, org, font, style.font_scale, color, style.thickness, cv2.LINE_AA)


def _draw_box(img, box: Box, style: _Style) -> None:
    import cv2

    color = BOX_BGR.get(box.color, (255, 255, 255))
    x0, y0, x1, y1 = box.bbox
    if box.dashed:
        _dashed_rect(img, (x0, y0), (x1, y1), color, style.thickness)
    else:
        cv2.rectangle(img, (x0, y0), (x1, y1), color, style.thickness)
    label = box.label if box.confidence is None else f"{box.label} {box.confidence:.2f}"
    _text(img, label, (x0, y0 - 2), color, style)


def _dashed_rect(img, p0, p1, color, thickness: int, dash: int = 8) -> None:
    import cv2

    (x0, y0), (x1, y1) = p0, p1
    for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        length = max(abs(b[0] - a[0]), abs(b[1] - a[1]), 1)
        for s in range(0, length, 2 * dash):
            f0, f1 = s / length, min(s + dash, length) / length
            q0 = (int(a[0] + (b[0] - a[0]) * f0), int(a[1] + (b[1] - a[1]) * f0))
            q1 = (int(a[0] + (b[0] - a[0]) * f1), int(a[1] + (b[1] - a[1]) * f1))
            cv2.line(img, q0, q1, color, thickness)


def _draw_crosshair(img, uv: UV, style: _Style) -> None:
    import cv2

    x, y = _pt(uv)
    r = style.marker
    cv2.line(img, (x - r, y), (x + r, y), TARGET_BGR, style.thickness, cv2.LINE_AA)
    cv2.line(img, (x, y - r), (x, y + r), TARGET_BGR, style.thickness, cv2.LINE_AA)
    cv2.circle(img, (x, y), r // 2, TARGET_BGR, style.thickness, cv2.LINE_AA)
    _text(img, "target", (x + r + 2, y + r), TARGET_BGR, style)


def _draw_gripper(img, uv: UV, style: _Style) -> None:
    import cv2

    x, y = _pt(uv)
    r = max(3, style.marker // 2)
    cv2.circle(img, (x, y), r, GRIPPER_BGR, -1, cv2.LINE_AA)
    cv2.circle(img, (x, y), r, (255, 255, 255), 1, cv2.LINE_AA)
    _text(img, "gripper", (x + style.marker, y - 2), GRIPPER_BGR, style)


def _draw_arrow(img, arrow: Tuple[UV, UV, str], style: _Style) -> None:
    import cv2

    start, end, label = arrow
    p0, p1 = _pt(start), _pt(end)
    tip = min(0.3, style.marker / max(math.hypot(p1[0] - p0[0], p1[1] - p0[1]), 1.0))
    cv2.arrowedLine(img, p0, p1, ERROR_BGR, style.thickness + 1, cv2.LINE_AA, tipLength=tip)
    mid = ((p0[0] + p1[0]) // 2 + style.pad, (p0[1] + p1[1]) // 2 + style.pad)
    _text(img, label, mid, ERROR_BGR, style, anchor="top")
