"""Camera geometry: calibration and ray casting. OWNER: WS3 (WS3.2–WS3.3).

Why not a homography: it is exact only on the table plane. A cup's top is 8 cm up, so
parallax gives ~2 cm error (≈4 cm for a stacked cup). Ray casting at the object's known
height removes it. See docs/DECISIONS.md "3D positions".

Contract:
- `calibrate(frames, marker_world_xy, checkerboard, marker_size=0.04) -> CameraModel`:
  `frames = {"checkerboard": [bgr, ...], "markers": bgr}`, `checkerboard = {"pattern": (cols, rows)
  inner corners, "square": m}`. Intrinsics from the printed checkerboard (`calibrate_intrinsics`),
  pose from ArUco markers (DICT_4X4_50) at robot-frame points measured by touching them with the
  gripper tip (`calibrate_pose`). Each marker is taped with its printed TOP edge facing +x (away from
  the robot base); `marker_size` is the black square's edge in metres.
- `CameraModel.save(path)` writes config/camera.json:
  {"K": [[fx,0,cx],[0,fy,cy],[0,0,1]], "dist": [k1,k2,p1,p2,k3], "R": 3x3, "t": [x,y,z],
   "markers": {"0": [0.10, -0.15], ...}, "created": "2026-10-24T12:00:00", "size": [w, h],
   "marker_size": 0.04, "reprojection_px": 0.4}
  with X_cam = R @ X_robot + t (OpenCV's solvePnP convention).
- `CameraModel.load(path)`.
- `CameraModel.pixel_to_table(u, v, z_plane) -> (x, y)`: intersect the (undistorted) pixel's ray with z = z_plane.
- `CameraModel.project(x, y, z) -> (u, v)`: robot frame → pixel, with lens distortion (annotations).
- `CameraModel.marker_errors(frame) -> {id: metres}`: re-detected marker centres vs their measured
  positions (the calibration check); `marker_drift(frame)` (WS3.6) will build on it.
Import cv2/numpy lazily.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

MARKER_SIZE = 0.04
MARKER_DICT = "DICT_4X4_50"
MIN_BOARD_VIEWS = 8
MIN_MARKERS = 3


class CalibrationError(RuntimeError):
    """Calibration could not be done, with what is missing."""


class CalibrationMissing(CalibrationError, NotImplementedError):
    """No config/camera.json on this machine: the calibration step has not been done here yet.
    Also a NotImplementedError so callers that skip backends not built yet (QA conformance) skip it."""


@dataclass
class CameraModel:
    K: List[List[float]]
    dist: List[float]
    R: List[List[float]]
    t: List[float]
    markers: Dict[str, List[float]] = field(default_factory=dict)
    size: Optional[List[int]] = None
    marker_size: float = MARKER_SIZE
    reprojection_px: Optional[float] = None
    created: str = ""

    @classmethod
    def load(cls, path: Union[str, Path]) -> CameraModel:
        if not Path(path).is_file():
            raise CalibrationMissing(f"no camera calibration at {path}: run tools/calibrate_camera.py "
                                     "(docs/RUNBOOK.md, camera calibration)")
        data = json.loads(Path(path).read_text())
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)

    def save(self, path: Union[str, Path]) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=1) + "\n")
        return path

    def project(self, x: float, y: float, z: float) -> Tuple[float, float]:
        import cv2
        import numpy as np

        rvec, _ = cv2.Rodrigues(np.array(self.R, float))
        uv, _ = cv2.projectPoints(np.array([[x, y, z]], float), rvec, np.array(self.t, float),
                                  np.array(self.K, float), np.array(self.dist, float))
        u, v = uv.reshape(2)
        return float(u), float(v)

    def pixel_to_table(self, u: float, v: float, z_plane: float) -> Tuple[float, float]:
        import cv2
        import numpy as np

        R, t = np.array(self.R, float), np.array(self.t, float)
        norm = cv2.undistortPoints(np.array([[[u, v]]], float), np.array(self.K, float), np.array(self.dist, float))
        ray = R.T @ np.array([norm[0, 0, 0], norm[0, 0, 1], 1.0])
        centre = -R.T @ t
        if abs(ray[2]) < 1e-9:
            raise ValueError(f"pixel ({u}, {v}) looks parallel to the plane z = {z_plane}")
        s = (z_plane - centre[2]) / ray[2]
        x, y, _ = centre + s * ray
        return float(x), float(y)

    def marker_drift(self, frame_bgr) -> float:
        raise NotImplementedError("WS3.6")

    def marker_errors(self, frame_bgr) -> Dict[int, float]:
        """Distance (m) between each re-detected marker centre (on the table) and its measured position."""
        found = detect_markers(frame_bgr)
        errors = {}
        for marker_id, corners in found.items():
            if str(marker_id) in self.markers:
                u, v = corners.mean(axis=0)
                x, y = self.pixel_to_table(float(u), float(v), 0.0)
                mx, my = self.markers[str(marker_id)]
                errors[marker_id] = math.hypot(x - mx, y - my)
        return errors


def detect_markers(frame_bgr) -> Dict[int, object]:
    """{id: 4x2 corner pixels (TL, TR, BR, BL of the printed marker)}."""
    import cv2

    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, MARKER_DICT))
    detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
    corners, ids, _ = detector.detectMarkers(frame_bgr)
    if ids is None:
        return {}
    return {int(i): c.reshape(4, 2) for i, c in zip(ids.flatten(), corners)}


def marker_corners_world(x: float, y: float, size: float) -> List[Tuple[float, float, float]]:
    """TL, TR, BR, BL of a marker centred at (x, y) on the table, its printed top edge facing +x."""
    h = size / 2
    return [(x + h, y + h, 0.0), (x + h, y - h, 0.0), (x - h, y - h, 0.0), (x - h, y + h, 0.0)]


def calibrate_intrinsics(frames_bgr: Sequence, pattern: Tuple[int, int], square: float):
    """(K, dist, rms px) from checkerboard views; `pattern` counts inner corners (cols, rows)."""
    import cv2
    import numpy as np

    board = np.zeros((pattern[0] * pattern[1], 3), np.float32)
    board[:, :2] = np.mgrid[0:pattern[0], 0:pattern[1]].T.reshape(-1, 2) * square
    object_pts, image_pts, size = [], [], None
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1e-3)
    for frame in frames_bgr:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        size = gray.shape[::-1]
        ok, corners = cv2.findChessboardCorners(gray, pattern)
        if ok:
            object_pts.append(board)
            image_pts.append(cv2.cornerSubPix(gray, corners, (5, 5), (-1, -1), criteria))
    if len(image_pts) < MIN_BOARD_VIEWS:
        raise CalibrationError(f"checkerboard {pattern[0]}x{pattern[1]} found in {len(image_pts)} of "
                               f"{len(frames_bgr)} views; need at least {MIN_BOARD_VIEWS} (tilt it, fill the frame)")
    rms, K, dist, _, _ = cv2.calibrateCamera(object_pts, image_pts, size, None, None)
    return K.tolist(), dist.reshape(-1)[:5].tolist(), float(rms)


def calibrate_pose(frame_bgr, K, dist, marker_world_xy: Mapping[int, Tuple[float, float]],
                   marker_size: float = MARKER_SIZE) -> CameraModel:
    """Camera pose in the robot frame from the ArUco markers in one overhead frame."""
    import cv2
    import numpy as np

    found = detect_markers(frame_bgr)
    usable = sorted(set(found) & {int(k) for k in marker_world_xy})
    if len(usable) < MIN_MARKERS:
        missing = sorted(set(int(k) for k in marker_world_xy) - set(found))
        raise CalibrationError(f"saw {len(usable)} of the measured markers (need {MIN_MARKERS}); "
                               f"not detected: {', '.join(map(str, missing))}")
    world = {int(k): v for k, v in marker_world_xy.items()}
    obj = np.array([p for i in usable for p in marker_corners_world(*world[i], marker_size)], float)
    img = np.array([p for i in usable for p in found[i]], float)
    K_, dist_ = np.array(K, float), np.array(dist, float)
    ok, rvec, tvec = cv2.solvePnP(obj, img, K_, dist_, flags=cv2.SOLVEPNP_IPPE)
    if not ok:
        raise CalibrationError("solvePnP failed on the detected markers")
    rvec, tvec = cv2.solvePnPRefineLM(obj, img, K_, dist_, rvec, tvec)
    reproj, _ = cv2.projectPoints(obj, rvec, tvec, K_, dist_)
    rms = float(np.sqrt(np.mean(np.sum((reproj.reshape(-1, 2) - img) ** 2, axis=1))))
    h, w = frame_bgr.shape[:2]
    return CameraModel(K=[list(map(float, r)) for r in K_], dist=list(map(float, dist_)),
                       R=cv2.Rodrigues(rvec)[0].tolist(), t=tvec.reshape(3).tolist(),
                       markers={str(i): list(world[i]) for i in sorted(world)}, size=[w, h],
                       marker_size=marker_size, reprojection_px=rms,
                       created=datetime.now().isoformat(timespec="seconds"))


def calibrate(frames: Mapping, marker_world_xy: Mapping[int, Tuple[float, float]], checkerboard: Mapping,
              marker_size: float = MARKER_SIZE) -> CameraModel:
    K, dist, _ = calibrate_intrinsics(frames["checkerboard"], tuple(checkerboard["pattern"]),
                                      float(checkerboard["square"]))
    return calibrate_pose(frames["markers"], K, dist, marker_world_xy, marker_size)
