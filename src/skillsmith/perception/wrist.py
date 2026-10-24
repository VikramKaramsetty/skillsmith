"""Wrist-camera checks. OWNER: WS3c (WS3.7).

Two pure functions over one wrist frame (BGR uint8, H x W x 3, as decoded from robotd's
`GET /frame/wrist`); the WorldModel's `holding_check()` / `align_offset(obj)` delegate here:

- `check_holding(frame, color, cfg) -> HoldingResult`: after a grasp, is the object between the
  fingers? Measures the fraction of the finger-gap ROI that has the object's colour (any
  saturated colour when the colour is unknown) and compares it with `wrist.min_fill`.
- `fine_offset(frame, color, cfg, distance=None) -> AlignResult`: while hovering, where is the
  object's centroid relative to the point straight under the gripper (`wrist.center`)? Pixels
  become metres with `wrist.px_per_m` (measured at `wrist.ref_distance`, scaled by
  ref_distance / distance when the camera-to-object distance is known), then image axes map to
  the tool frame (`u_axis`, `v_axis`). `tool_to_base()` rotates that into the robot base frame
  (what `robot.align_over` adds to the tip). Corrections are clamped to `wrist.max_offset`.

`holding_check()` / `align_offset()` give the contract shapes ((bool, str) and (dx, dy)).
Geometry still decides pass/fail; these are fine corrections and a second opinion on the grasp.

Optional config section (defaults below apply when it is absent; MEASURE on the arm):
    wrist:
      roi: [0.35, 0.45, 0.65, 0.95]   # finger gap as fractions of the image (x0, y0, x1, y1)
      min_fill: 0.25                  # ROI fraction of object colour that counts as holding
      min_s: 90, min_v: 40            # HSV floor for "coloured" pixels
      min_mean_v: 25                  # darker ROI than this = cannot judge
      min_area: 150                   # px; smallest blob accepted for alignment
      px_per_m: 4000                  # image scale at ref_distance (camera to object top)
      ref_distance: 0.10              # m
      center: [0.5, 0.5]              # pixel (fractions) straight under the gripper tip
      u_axis: "-y"                    # tool axis for image +u (right): one of +x -x +y -y
      v_axis: "-x"                    # tool axis for image +v (down)
      max_offset: 0.02                # m; larger corrections are clamped (and flagged)
      roll_sign: 1.0                  # sign of wrist_roll in the tool yaw (verify on the arm)
      hues: {...}                     # OpenCV hue ranges per colour; else detect.hsv.hues
Imports cv2/numpy lazily: core tests must run without them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

# OpenCV hue is 0..179; red wraps, so it gets two ranges. Same defaults as the HSV detector.
DEFAULT_HUES: dict[str, list[tuple[int, int]]] = {
    "red": [(0, 9), (170, 179)],
    "orange": [(10, 21)],
    "yellow": [(22, 34)],
    "green": [(35, 85)],
    "blue": [(95, 130)],
    "purple": [(131, 169)],
}
AXES: dict[str, tuple[float, float]] = {"+x": (1.0, 0.0), "-x": (-1.0, 0.0), "+y": (0.0, 1.0), "-y": (0.0, -1.0)}


@dataclass(frozen=True)
class WristConfig:
    roi: tuple[float, float, float, float] = (0.35, 0.45, 0.65, 0.95)
    min_fill: float = 0.25
    min_s: int = 90
    min_v: int = 40
    min_mean_v: float = 25.0
    min_area: int = 150
    px_per_m: float = 4000.0
    ref_distance: float = 0.10
    center: tuple[float, float] = (0.5, 0.5)
    u_axis: str = "-y"
    v_axis: str = "-x"
    max_offset: float = 0.02
    roll_sign: float = 1.0
    hues: dict[str, list[tuple[int, int]]] = field(default_factory=lambda: dict(DEFAULT_HUES))

    def __post_init__(self) -> None:
        x0, y0, x1, y1 = self.roi
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            raise ValueError(f"wrist.roi must be fractions x0<x1, y0<y1 in [0, 1], got {self.roi}")
        if self.px_per_m <= 0 or self.ref_distance <= 0:
            raise ValueError("wrist.px_per_m and wrist.ref_distance must be > 0")
        if not 0.0 < self.min_fill < 1.0:
            raise ValueError(f"wrist.min_fill must be in (0, 1), got {self.min_fill}")
        for name in (self.u_axis, self.v_axis):
            if name not in AXES:
                raise ValueError(f"wrist axis must be one of {sorted(AXES)}, got {name!r}")
        if AXES[self.u_axis][0] == 0.0 and AXES[self.v_axis][0] == 0.0:
            raise ValueError("wrist.u_axis and wrist.v_axis must be different axes (one x, one y)")
        if AXES[self.u_axis][1] == 0.0 and AXES[self.v_axis][1] == 0.0:
            raise ValueError("wrist.u_axis and wrist.v_axis must be different axes (one x, one y)")

    @classmethod
    def from_cfg(cls, cfg: Any = None) -> WristConfig:
        """Build from a Config, a plain dict (whole robot.yaml) or None (all defaults)."""
        if isinstance(cfg, WristConfig):
            return cfg
        data = cfg.raw() if hasattr(cfg, "raw") else (cfg or {})
        section = dict(data.get("wrist") or {}) if isinstance(data, dict) else {}
        hsv = ((data.get("detect") or {}).get("hsv") or {}) if isinstance(data, dict) else {}
        hues = section.pop("hues", None) or hsv.get("hues") or DEFAULT_HUES
        known = {k: v for k, v in section.items() if k in cls.__dataclass_fields__}
        for key in ("roi", "center"):
            if key in known:
                known[key] = tuple(float(v) for v in known[key])
        return cls(**known, hues={str(k): [tuple(r) for r in v] for k, v in hues.items()})


@dataclass(frozen=True)
class HoldingResult:
    holding: bool
    confidence: float  # 0..1; 0 = could not judge (dark frame)
    fill: float  # fraction of the finger-gap ROI with the object's colour
    reason: str


@dataclass(frozen=True)
class AlignResult:
    found: bool
    dx: float  # tool frame, metres (clamped to max_offset)
    dy: float
    du: float  # centroid minus target pixel
    dv: float
    confidence: float  # 0..1
    reason: str


# ---------------------------------------------------------------- pure geometry (no numpy)


def roi_box(width: int, height: int, roi: Sequence[float]) -> tuple[int, int, int, int]:
    """Fractional (x0, y0, x1, y1) -> pixel box, x1/y1 exclusive, never empty."""
    x0, y0 = int(round(roi[0] * width)), int(round(roi[1] * height))
    x1, y1 = int(round(roi[2] * width)), int(round(roi[3] * height))
    return x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)


def hue_ranges(color: Optional[str], wc: WristConfig) -> Optional[list[tuple[int, int]]]:
    """Hue ranges for a colour word ("blue", "light blue" -> blue); None = unknown colour."""
    words = (color or "").strip().lower().split()
    if not words:
        return None
    return wc.hues.get(" ".join(words)) or wc.hues.get(words[-1])


def pixel_to_tool(du: float, dv: float, wc: WristConfig, distance: Optional[float] = None) -> tuple[float, float]:
    """Pixel error (image right/down) -> tool-frame metres, scaled for the camera distance."""
    px_per_m = wc.px_per_m * (wc.ref_distance / distance if distance and distance > 0 else 1.0)
    u_m, v_m = du / px_per_m, dv / px_per_m
    ux, uy = AXES[wc.u_axis]
    vx, vy = AXES[wc.v_axis]
    return u_m * ux + v_m * vx, u_m * uy + v_m * vy


def clamp_offset(dx: float, dy: float, limit: float) -> tuple[float, float, bool]:
    """Scale (dx, dy) down to length `limit` if longer; returns (dx, dy, clamped)."""
    norm = math.hypot(dx, dy)
    if limit <= 0 or norm <= limit:
        return dx, dy, False
    return dx * limit / norm, dy * limit / norm, True


def tool_yaw_deg(joints: Sequence[float], wc: Optional[WristConfig] = None) -> float:
    """Tool yaw in the base frame with the gripper pointing down: shoulder_pan + wrist_roll (deg).
    The roll sign depends on the calibration (WS2.7): set wrist.roll_sign after checking."""
    sign = (wc or WristConfig()).roll_sign
    return float(joints[0]) + sign * float(joints[4])


def tool_to_base(dx: float, dy: float, yaw_deg: float) -> tuple[float, float]:
    """Rotate a tool-frame xy offset into the robot base frame (yaw about +z, degrees)."""
    c, s = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    return c * dx - s * dy, s * dx + c * dy


# ---------------------------------------------------------------- image checks (cv2 + numpy)


def _check_frame(frame_bgr: Any) -> tuple[int, int]:
    shape = getattr(frame_bgr, "shape", None)
    if shape is None or len(shape) != 3 or shape[2] != 3 or shape[0] < 2 or shape[1] < 2:
        raise ValueError(f"wrist frame must be an H x W x 3 BGR image, got shape {shape}")
    return int(shape[1]), int(shape[0])


def colour_mask(frame_bgr: Any, color: Optional[str], wc: WristConfig):
    """Boolean mask of pixels with the object's colour (any saturated colour if unknown)."""
    import cv2
    import numpy as np

    hsv = cv2.cvtColor(np.ascontiguousarray(frame_bgr, dtype=np.uint8), cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    mask = (s >= wc.min_s) & (v >= wc.min_v)
    ranges = hue_ranges(color, wc)
    if ranges is None:
        return mask
    in_hue = np.zeros_like(mask)
    for lo, hi in ranges:
        in_hue |= (h >= lo) & (h <= hi)
    return mask & in_hue


def check_holding(frame_bgr: Any, color: Optional[str] = None, cfg: Any = None) -> HoldingResult:
    """Is an object of `color` filling the finger gap? Confidence grows with the margin from
    `min_fill`; it is lower when the colour is unknown and 0 when the ROI is too dark."""
    import numpy as np

    wc = WristConfig.from_cfg(cfg)
    width, height = _check_frame(frame_bgr)
    x0, y0, x1, y1 = roi_box(width, height, wc.roi)
    roi = np.asarray(frame_bgr)[y0:y1, x0:x1]
    mean_v = float(roi.max(axis=2).mean())  # HSV value = max(B, G, R)
    if mean_v < wc.min_mean_v:
        return HoldingResult(False, 0.0, 0.0, f"wrist frame too dark (mean V {mean_v:.0f} < {wc.min_mean_v:.0f})")
    fill = float(colour_mask(roi, color, wc).mean())
    holding = fill >= wc.min_fill
    margin = min(1.0, abs(fill - wc.min_fill) / wc.min_fill)
    known = hue_ranges(color, wc) is not None
    confidence = round((0.5 + 0.5 * margin) * (1.0 if known else 0.7), 3)
    what = color if known else "a coloured object"
    verdict = "holding" if holding else "not holding"
    reason = f"{verdict} {what}: {fill:.0%} of the finger gap matches (need {wc.min_fill:.0%})"
    return HoldingResult(holding, confidence, round(fill, 4), reason)


def _largest_blob(mask: Any, min_area: int) -> Optional[tuple[float, float, int, bool]]:
    """(cx, cy, area, touches_border) of the biggest blob of at least `min_area` px, or None."""
    import cv2
    import numpy as np

    clean = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(clean, connectivity=8)
    if count <= 1:
        return None
    best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h, area = (int(v) for v in stats[best])
    if area < min_area:
        return None
    border = x == 0 or y == 0 or x + w >= mask.shape[1] or y + h >= mask.shape[0]
    return float(centroids[best][0]), float(centroids[best][1]), area, border


def fine_offset(frame_bgr: Any, color: Optional[str] = None, cfg: Any = None,
                distance: Optional[float] = None) -> AlignResult:
    """Tool-frame (dx, dy) in metres that moves the gripper over the object's centroid.
    `distance` is the camera-to-object-top distance (m) when known; it rescales px_per_m."""
    wc = WristConfig.from_cfg(cfg)
    width, height = _check_frame(frame_bgr)
    blob = _largest_blob(colour_mask(frame_bgr, color, wc), wc.min_area)
    what = color or "coloured object"
    if blob is None:
        return AlignResult(False, 0.0, 0.0, 0.0, 0.0, 0.0, f"no {what} in the wrist view (min {wc.min_area} px)")
    cx, cy, area, border = blob
    du, dv = cx - wc.center[0] * (width - 1), cy - wc.center[1] * (height - 1)
    raw_dx, raw_dy = pixel_to_tool(du, dv, wc, distance)
    dx, dy, clamped = clamp_offset(raw_dx, raw_dy, wc.max_offset)
    confidence = min(1.0, area / (4.0 * wc.min_area)) * (0.5 if border else 1.0) * (0.5 if clamped else 1.0)
    notes = [f"{what} {du:+.0f},{dv:+.0f} px from centre -> {dx * 100:+.1f},{dy * 100:+.1f} cm (tool)"]
    if border:
        notes.append("blob touches the image edge (partial view)")
    if clamped:
        notes.append(f"clamped from {math.hypot(raw_dx, raw_dy) * 100:.1f} cm to {wc.max_offset * 100:.1f} cm")
    return AlignResult(True, round(dx, 4), round(dy, 4), round(du, 1), round(dv, 1), round(confidence, 3),
                       "; ".join(notes))


# ---------------------------------------------------------------- contract-shaped wrappers


def holding_check(frame_bgr: Any, color: Optional[str] = None, cfg: Any = None) -> tuple[bool, str]:
    """`Perception.holding_check()` shape: (holding, reason)."""
    result = check_holding(frame_bgr, color, cfg)
    return result.holding, f"{result.reason} (confidence {result.confidence:.2f})"


def align_offset(frame_bgr: Any, color: Optional[str] = None, cfg: Any = None, yaw_deg: float = 0.0,
                 distance: Optional[float] = None) -> tuple[float, float]:
    """`Perception.align_offset(obj)` shape: base-frame (dx, dy) in metres; (0, 0) if not found."""
    result = fine_offset(frame_bgr, color, cfg, distance)
    if not result.found:
        return 0.0, 0.0
    dx, dy = tool_to_base(result.dx, result.dy, yaw_deg)
    return round(dx, 4), round(dy, 4)
