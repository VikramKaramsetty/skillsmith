"""Camera geometry: calibration and ray casting. OWNER: WS3 (WS3.2–WS3.3).

Why not a homography: it is exact only on the table plane. A cup's top is 8 cm up, so
parallax gives ~2 cm error (≈4 cm for a stacked cup). Ray casting at the object's known
height removes it. See docs/DECISIONS.md "3D positions".

Contract:
- `calibrate(frames, marker_world_xy, checkerboard) -> CameraModel`: intrinsics from a printed
  checkerboard, pose from ArUco markers at robot-frame points measured by touching them with
  the gripper tip (FK). Saves config/camera.json:
  {"K": [[fx,0,cx],[0,fy,cy],[0,0,1]], "dist": [k1,k2,p1,p2,k3], "R": 3x3, "t": [x,y,z],
   "markers": {"0": [0.10, -0.15], ...}, "created": "2026-10-24T12:00:00"}
- `CameraModel.load(path)`.
- `CameraModel.pixel_to_table(u, v, z_plane) -> (x, y)`: intersect the pixel's ray with z = z_plane.
- `CameraModel.project(x, y, z) -> (u, v)`: robot frame → pixel (used for annotations).
- `CameraModel.marker_drift(frame) -> float`: max marker displacement in metres (confidence gate).
Import cv2/numpy lazily.
"""

from __future__ import annotations

from typing import Tuple


class CameraModel:
    @classmethod
    def load(cls, path: str) -> CameraModel:
        raise NotImplementedError("WS3.2: see docs/workstreams/WS3_perception_world.md")

    def pixel_to_table(self, u: float, v: float, z_plane: float) -> Tuple[float, float]:
        raise NotImplementedError("WS3.3")

    def project(self, x: float, y: float, z: float) -> Tuple[float, float]:
        raise NotImplementedError("WS3.3")

    def marker_drift(self, frame_bgr) -> float:
        raise NotImplementedError("WS3.6")


def calibrate(frames, marker_world_xy, checkerboard) -> CameraModel:
    raise NotImplementedError("WS3.2")
