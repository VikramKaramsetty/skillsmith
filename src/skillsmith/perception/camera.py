"""CameraPerception. OWNER: WS3 (docs/workstreams/WS3_perception.md). Used by hal/real.py (WS1.4).

Contract (docs/INTERFACES.md §2, `Perception` protocol):
- `CameraPerception(cfg, frame_source)`: `frame_source.read() -> BGR image` (WS3.1 provides
  sources: robotd `/frame/front` client, OpenCV device, MJPEG URL, and a fixture-folder source for tests).
- `observe() -> Scene` with `source="camera:front"`: `perception.color.detect(frame, homography,
  known_objects)` using `cfg.cameras.homography` (written by tools/calibrate_camera.py, WS3.2).
- `snapshot(path) -> path`: save the latest frame as JPEG (goes into Droid's workspace and the dashboard).
- Under 200 ms per `observe()`. When robotd is running, read frames through robotd only (it owns the camera).
"""

from __future__ import annotations

from typing import Optional

from skillsmith.types import Scene


class CameraPerception:
    def __init__(self, cfg, frame_source=None):
        raise NotImplementedError("WS3.5: see docs/workstreams/WS3_perception.md")

    def observe(self) -> Scene:
        raise NotImplementedError("WS3.5")

    def snapshot(self, path: str) -> Optional[str]:
        raise NotImplementedError("WS3.5")
