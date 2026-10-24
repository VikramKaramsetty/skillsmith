"""Object detectors. OWNER: WS3 (WS3.4).

Contract (one interface, two implementations):
- `Detection(label, color, kind, bbox, point_uv, confidence)`: `point_uv` is the pixel of the
  object's TOP centre (what an overhead camera sees); the world model ray-casts it at the
  object's known top height (perception/geometry.py).
- `HsvDetector(cfg).detect(frame_bgr) -> list[Detection]`: colour blobs; the backup, fast and
  robust for saturated matte objects. Thresholds tuned on site with tools/hsv_tuner.py.
- `YoloWorldDetector(cfg, labels).detect(frame_bgr) -> list[Detection]`: YOLO-World / YOLO-E
  open-vocabulary on the GX10 GPU; the main detector.
- Object sizes come from a known-object table (cup 0.08/0.065 m, block 0.03 m; measure yours),
  never from pixels.
Imports cv2/numpy/ultralytics lazily: core tests must run without them.
"""

from __future__ import annotations


class HsvDetector:
    def __init__(self, cfg):
        raise NotImplementedError("WS3.4: see docs/workstreams/WS3_perception_world.md")

    def detect(self, frame_bgr):
        raise NotImplementedError("WS3.4")


class YoloWorldDetector:
    def __init__(self, cfg, labels):
        raise NotImplementedError("WS3.4")

    def detect(self, frame_bgr):
        raise NotImplementedError("WS3.4")
