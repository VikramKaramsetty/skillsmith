"""Object detectors. OWNER: WS3b (WS3.4).

Contract (one interface, two implementations):
- `Detection(label, color, kind, bbox, point_uv, confidence, source)`: `point_uv` is the pixel of
  the object's TOP centre (what an overhead camera sees); the world model ray-casts it at the
  object's known top height (perception/geometry.py). `bbox` is (x0, y0, x1, y1) in pixels.
- `HsvDetector(cfg, labels).detect(frame_bgr) -> list[Detection]`: colour blobs; the backup, fast
  and robust for saturated matte objects. Needs only opencv + numpy (the `vision` extra).
- `YoloWorldDetector(cfg, labels).detect(frame_bgr) -> list[Detection]`: YOLO-World / YOLO-E
  open-vocabulary (Apple-silicon GPU via MPS); the main detector. Needs `ultralytics` (not a declared extra).
- `FallbackDetector(primary, backup)`: the main detector, with the backup covering a crash or a
  missed label. `make_detector(cfg, labels)` builds the best one available.
- Object sizes come from a known-object table (cup 0.08/0.065 m, block 0.03 m; measure yours),
  never from pixels.

Confidence (0..1) is what the world model's gate compares with `world.min_confidence`:
- HSV: solidity of the object's top face (an unoccluded rim or block top is convex, ~1.0; a
  crescent left visible under a stacked cup is ~0.5) times a size factor (tiny blobs score low).
- YOLO: the model's own score.

Optional config section (defaults below apply when it is absent):
    detect:
      backend: auto            # auto (YOLO if it loads, HSV backup) | yolo | hsv
      hsv: {min_s: 90, min_v: 40, min_area: 120, top_frac: 0.85, hues: {red: [[0, 9], [170, 179]], ...}}
      yolo: {model: yolov8s-worldv2.pt, conf: 0.25}
Imports cv2/numpy/ultralytics lazily: core tests must run without them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, Sequence

# OpenCV hue is 0..179. Red wraps around 0, so it gets two ranges. Tune on site.
DEFAULT_HUES: dict[str, list[tuple[int, int]]] = {
    "red": [(0, 9), (170, 179)],
    "orange": [(10, 21)],
    "yellow": [(22, 34)],
    "green": [(35, 85)],
    "blue": [(95, 130)],
    "purple": [(131, 169)],
}
DEFAULT_HSV = {"min_s": 90, "min_v": 40, "min_area": 120, "top_frac": 0.85, "cup_hole": 0.3}
DEFAULT_YOLO = {"model": "yolov8s-worldv2.pt", "conf": 0.25}


@dataclass(frozen=True)
class Detection:
    label: str  # "blue cup"
    color: str  # "blue" ("" when the label has no colour word)
    kind: str  # "cup"
    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1 (pixels, inclusive)
    point_uv: tuple[float, float]  # pixel of the TOP centre
    confidence: float  # 0..1
    source: str = "hsv"  # "hsv" | "yolo"


class Detector(Protocol):
    def detect(self, frame_bgr) -> list[Detection]: ...


def split_label(label: str) -> tuple[str, str]:
    """"blue cup" -> ("blue", "cup"); "cup" -> ("", "cup"); "light blue cup" -> ("light blue", "cup")."""
    words = label.strip().lower().split()
    if not words:
        return "", ""
    return " ".join(words[:-1]), words[-1]


def _section(cfg: Any, *keys: str) -> dict:
    """cfg.detect.<keys> as a plain dict; accepts a Config, a dict or None."""
    data = cfg.raw() if hasattr(cfg, "raw") else (cfg or {})
    for key in ("detect", *keys):
        data = (data.get(key) or {}) if isinstance(data, dict) else {}
    return dict(data)


# ---------------------------------------------------------------- HSV backup


class HsvDetector:
    """Saturated colour blobs. With `labels`, only those colours are reported and the kind comes
    from the label; without, every known colour is reported and the kind comes from the shape
    (a top face with a dark hole in it is a cup, a solid one a block)."""

    def __init__(self, cfg: Any = None, labels: Optional[Sequence[str]] = None):
        hsv = _section(cfg, "hsv")
        hues = hsv.pop("hues", None) or DEFAULT_HUES
        self.params = {**DEFAULT_HSV, **hsv}
        self.hues = {name: [tuple(r) for r in ranges] for name, ranges in hues.items()}
        self.kinds: dict[str, set[str]] = {}
        self.unseen: list[str] = []  # labels with no hue range: only YOLO can find these
        for label in labels or ():
            color, kind = split_label(label)
            if color in self.hues:
                self.kinds.setdefault(color, set()).add(kind)
            else:
                self.unseen.append(label)
        self.colors = list(self.kinds) if labels else list(self.hues)

    def detect(self, frame_bgr) -> list[Detection]:
        import cv2

        h, s, v = cv2.split(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV))
        found = [d for color in self.colors for d in self._blobs(color, h, s, v)]
        return sorted(found, key=lambda d: -d.confidence)

    def _blobs(self, color: str, h, s, v) -> list[Detection]:
        import cv2
        import numpy as np

        p = self.params
        mask = (hue_mask(h, self.hues[color]) & (s >= p["min_s"]) & (v >= p["min_v"])).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        out = []
        for i in range(1, n):
            x, y, w, hh, area = (int(a) for a in stats[i])
            if area < p["min_area"]:
                continue
            top = top_face(labels == i, v, p["top_frac"])
            if top is None:
                continue
            point, solidity, hole = top
            kind = self._kind(color, hole)
            size = min(1.0, area / (3.0 * p["min_area"]))
            confidence = round(max(0.0, min(1.0, solidity * size)), 3)
            out.append(Detection(f"{color} {kind}", color, kind, (x, y, x + w - 1, y + hh - 1), point, confidence, "hsv"))
        return out

    def _kind(self, color: str, hole: float) -> str:
        known = self.kinds.get(color, set())
        if len(known) == 1:
            return next(iter(known))
        shape = "cup" if hole >= self.params["cup_hole"] else "block"
        return shape if not known or shape in known else sorted(known)[0]


def hue_mask(h, ranges):
    """Pixels whose OpenCV hue (0..179) falls in any of the inclusive `ranges`."""
    import numpy as np

    mask = np.zeros(h.shape, bool)
    for lo, hi in ranges:
        mask |= (h >= lo) & (h <= hi)
    return mask


def top_face(blob, value, top_frac: float = 0.85):
    """The top face of one object's pixel mask: ((u, v) centroid, solidity, hole fraction), or None.
    Overhead, the top face (a cup's rim, a block's top) is the brightest part of the blob and the
    sides are shaded; a cup's dark inside is a hole in the rim, so holes are filled before the
    centroid is taken."""
    import cv2
    import numpy as np

    vals = value[blob]
    if vals.size == 0:
        return None
    bright = (blob & (value >= top_frac * np.percentile(vals, 95))).astype(np.uint8) * 255
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    outline = max(contours, key=cv2.contourArea)
    filled = np.zeros_like(bright)
    cv2.drawContours(filled, [outline], -1, 255, -1)
    m = cv2.moments(filled, True)
    if m["m00"] <= 0:
        return None
    hole = 1.0 - cv2.countNonZero(bright & filled) / cv2.countNonZero(filled)
    solidity = cv2.contourArea(outline) / max(cv2.contourArea(cv2.convexHull(outline)), 1.0)
    return (round(m["m10"] / m["m00"], 2), round(m["m01"] / m["m00"], 2)), min(1.0, solidity), hole


# ---------------------------------------------------------------- YOLO-World / YOLO-E main


class YoloWorldDetector:
    """Open-vocabulary detection prompted with the task's labels. `model` may be injected (tests,
    or a model already on the GPU); otherwise ultralytics loads `detect.yolo.model` lazily."""

    def __init__(self, cfg: Any, labels: Sequence[str], model: Any = None):
        if not labels:
            raise ValueError("YoloWorldDetector needs at least one label to look for")
        self.labels = [str(label) for label in labels]
        self.params = {**DEFAULT_YOLO, **_section(cfg, "yolo")}
        hsv = {**DEFAULT_HSV, **_section(cfg, "hsv")}
        self.hues = hsv.pop("hues", None) or DEFAULT_HUES
        self.hsv = hsv
        self.model = model if model is not None else _load_yolo(self.params["model"])
        _set_classes(self.model, self.labels)

    def detect(self, frame_bgr) -> list[Detection]:
        results = self.model.predict(frame_bgr, conf=float(self.params["conf"]), verbose=False)
        if not results:
            return []
        r = results[0]
        names = getattr(r, "names", None) or dict(enumerate(self.labels))
        boxes = r.boxes
        out = []
        for xyxy, conf, cls in zip(_as_list(boxes.xyxy), _as_list(boxes.conf), _as_list(boxes.cls)):
            label = str(names[int(cls)])
            color, kind = split_label(label)
            bbox = tuple(int(round(c)) for c in xyxy)
            point = self._top_point(frame_bgr, bbox, color)
            out.append(Detection(label, color, kind, bbox, point, round(float(conf), 3), "yolo"))
        return sorted(out, key=lambda d: -d.confidence)

    def _top_point(self, frame_bgr, bbox, color: str) -> tuple[float, float]:
        """The top centre inside the box, found with the colour's hue range when we know it; the
        box centre otherwise (a box includes the object's side, so that is the cruder estimate)."""
        x0, y0, x1, y1 = (max(int(c), 0) for c in bbox)
        centre = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
        if color not in self.hues or x1 <= x0 or y1 <= y0:
            return centre
        try:
            import cv2
        except ImportError:
            return centre
        crop = frame_bgr[y0 : y1 + 1, x0 : x1 + 1]
        if crop.size == 0:
            return centre
        h, s, v = cv2.split(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV))
        blob = hue_mask(h, self.hues[color]) & (s >= self.hsv["min_s"]) & (v >= self.hsv["min_v"])
        if blob.sum() < 0.1 * blob.size:
            return centre
        top = top_face(blob, v, self.hsv["top_frac"])
        if top is None:
            return centre
        (u, vv), _, _ = top
        return (round(u + x0, 2), round(vv + y0, 2))


def _load_yolo(path: str):
    try:
        import ultralytics
    except ImportError as e:
        raise ImportError("YoloWorldDetector needs `ultralytics` (pip install ultralytics)") from e
    if "yoloe" in str(path).lower():
        return ultralytics.YOLOE(path)
    return ultralytics.YOLOWorld(path)


def _set_classes(model: Any, labels: list[str]) -> None:
    if hasattr(model, "get_text_pe"):  # YOLO-E wants text embeddings with the names
        model.set_classes(labels, model.get_text_pe(labels))
    elif hasattr(model, "set_classes"):
        model.set_classes(labels)


def _as_list(x) -> list:
    """Tensor / ndarray / list -> plain list (tensor.tolist() also copies off the GPU)."""
    return x.tolist() if hasattr(x, "tolist") else list(x)


# ---------------------------------------------------------------- main + backup


class FallbackDetector:
    """Run `primary`; if it raises, use `backup` (one `error` event per outage, not per frame).
    Labels the primary missed but the backup found are added, so a weak YOLO prompt never
    hides an object the colour detector can see."""

    def __init__(self, primary: Detector, backup: Detector, bus: Any = None):
        self.primary, self.backup, self.bus = primary, backup, bus
        self.primary_down = False

    def detect(self, frame_bgr) -> list[Detection]:
        try:
            main = self.primary.detect(frame_bgr)
        except Exception as e:
            if not self.primary_down:
                self.primary_down = True
                _emit(self.bus, f"{type(self.primary).__name__} failed, using the backup: {e!r}")
            return self.backup.detect(frame_bgr)
        if self.primary_down:
            self.primary_down = False
            _emit(self.bus, f"{type(self.primary).__name__} recovered")
        seen = {d.label for d in main}
        extra = [d for d in self.backup.detect(frame_bgr) if d.label not in seen]
        return sorted(main + extra, key=lambda d: -d.confidence)


def make_detector(cfg: Any, labels: Optional[Sequence[str]] = None, bus: Any = None) -> Detector:
    """`detect.backend`: auto (default) = YOLO with the HSV backup, or HSV alone if YOLO can't
    load (an `error` event says why); `yolo` = YOLO with the backup, raising if it can't load;
    `hsv` = HSV only. YOLO needs labels to prompt with; without them auto means HSV."""
    backend = _section(cfg).get("backend", "auto")
    if backend not in ("auto", "yolo", "hsv"):
        raise ValueError(f"detect.backend must be auto, yolo or hsv, not '{backend}'")
    hsv = HsvDetector(cfg, labels)
    if backend == "hsv" or (backend == "auto" and not labels):
        return hsv
    try:
        yolo = YoloWorldDetector(cfg, labels or [])
    except Exception as e:
        if backend == "yolo":
            raise
        _emit(bus, f"YOLO detector unavailable, using HSV only: {e!r}")
        return hsv
    return FallbackDetector(yolo, hsv, bus)


def _emit(bus: Any, message: str) -> None:
    if bus is not None:
        bus.emit("error", where="perception.detector", message=message)
    else:
        print(f"[perception.detector] {message}")
