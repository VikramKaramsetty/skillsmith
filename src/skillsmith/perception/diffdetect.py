"""Model-free cup detector for a fixed overhead camera: background difference + base-rim circle fit.

For objects HSV cannot see (a white or printed cup on a white mat) when YOLO is not available:
- A reference image of the EMPTY table (`tools/capture_background.py`) is compared with each frame
  (at half resolution); pixels that differ enough (LAB distance > `diff_threshold`) form blobs.
- Each blob's outline is back-projected (calibrated camera) onto the table plane, and RANSAC fits the
  circle where the object meets the table. That height is known exactly, so (x, y) needs no height guess
  and the camera's tilt is undone. The top rim is not used: a white top barely differs from a white mat,
  and the edge of a cup's print looks like a rim at the wrong height (it put a cup 3 cm off).
- A blob is dropped when its rim is not a circle of a plausible radius covering 150+ degrees (labels,
  cables, hands), when it touches the frame edge, or when its centre is off the workspace box (+3 cm).
- `point_uv` is the base centre projected to the object's nominal top height, which is what the world
  model ray-casts. On the rig (2026-10-25): T1-T5 worst 0.52 cm, mean 0.27 cm, ~40 ms per frame.

Config (all optional): detect: {backend: diff, labels: ["white cup"], diff: {background: runs/background.png,
diff_threshold: 28, min_radius: 0.02, max_radius: 0.05, top_height: 0.08}}.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

from skillsmith.perception.color import Detection, _section, split_label

WORK_SCALE = 2  # the difference image is computed at half resolution
DEFAULT_DIFF = {"background": "runs/background.png", "diff_threshold": 28, "min_radius": 0.02,
                "max_radius": 0.05, "top_height": 0.08,  # radii: of the base rim (a cup's mouth when upside down)
                "inlier_tol": 0.004}


class DiffDetector:
    def __init__(self, cfg: Any, labels: Optional[Sequence[str]] = None, camera=None, background=None):
        import cv2

        self.params = {**DEFAULT_DIFF, **_section(cfg, "diff")}
        self.label = (list(labels) or ["white cup"])[0] if labels else "white cup"
        self.color, self.kind = split_label(self.label)
        if camera is None:
            from skillsmith.perception.geometry import CameraModel

            camera = CameraModel.load(str(_path(cfg, _cam_path(cfg))))
        self.camera = camera
        self.last_heights: dict = {}
        raw = cfg.raw() if hasattr(cfg, "raw") else (cfg or {})
        ws = raw.get("workspace") or {}
        m = float(self.params.get("margin", 0.03))
        self.box = ((ws.get("x", [-9, 9])[0] - m, ws.get("x", [-9, 9])[1] + m),
                    (ws.get("y", [-9, 9])[0] - m, ws.get("y", [-9, 9])[1] + m))  # off the mat = people, cables
        if background is None:
            background = cv2.imread(str(_path(cfg, self.params["background"])))
            if background is None:
                raise OSError(f"no background image at {self.params['background']}: "
                              "run tools/capture_background.py with the table empty")
        self.background = background
        self._bg_lab = self._lab(background)

    def detect(self, frame_bgr) -> List[Detection]:
        import cv2
        import numpy as np

        if frame_bgr.shape[:2] != self.background.shape[:2]:
            return []
        diff = np.linalg.norm(self._lab(frame_bgr) - self._bg_lab, axis=2)
        mask = (diff > float(self.params["diff_threshold"])).astype("uint8") * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        out = []
        for c in contours:
            if cv2.contourArea(c) < 100:  # half-resolution pixels
                continue
            x0, y0, w0, h0 = cv2.boundingRect(c)
            if x0 <= 1 or y0 <= 1 or x0 + w0 >= mask.shape[1] - 2 or y0 + h0 >= mask.shape[0] - 2:
                continue  # cut off by the frame edge: no full rim to fit (people, laptops at the edge)
            c = c * WORK_SCALE  # back to full-resolution pixels
            fit = self._fit(c.reshape(-1, 2).astype(float))
            if fit is None:
                continue
            (x, y), h, r, score = fit
            if not (self.box[0][0] <= x <= self.box[0][1] and self.box[1][0] <= y <= self.box[1][1]):
                continue
            self.last_heights[(round(x, 3), round(y, 3))] = h  # measured top z, for callers that want 3D
            u, v = self.camera.project(x, y, h)
            bx, by, bw, bh = cv2.boundingRect(c)
            out.append(Detection(self.label, self.color, self.kind, (bx, by, bx + bw - 1, by + bh - 1),
                                 (float(u), float(v)), round(score, 3), "diff"))
        return out

    @staticmethod
    def _lab(bgr):
        """Blurred LAB at 1/WORK_SCALE resolution: 4x less work, and the rim fit uses full-res coordinates."""
        import cv2

        small = cv2.resize(bgr, None, fx=1 / WORK_SCALE, fy=1 / WORK_SCALE, interpolation=cv2.INTER_AREA)
        return cv2.cvtColor(cv2.GaussianBlur(small, (3, 3), 0), cv2.COLOR_BGR2LAB).astype("float32")

    def _to_planes(self, uv, heights):
        """The pixels' table (x, y) on each plane z = height, as arrays."""
        centre, rays = _rays(self.camera, uv)
        out = []
        for z in heights:
            s = (z - centre[2]) / rays[:, 2]
            out.append(centre[:2] + s[:, None] * rays[:, :2])
        return out

    def _fit(self, pts_uv):
        """((x, y), top z, base radius, confidence) from the BASE rim, the circle where the object meets the
        table (z = 0, known exactly), so (x, y) needs no height guess.

        Why not the top rim: a white top on a white mat does not show in the difference image, and the
        upper edge of a printed cup's outline is where the print stops on the side wall, which looks like a
        rim at the wrong height (it put the T5 cup 3 cm off). The top z reported is the object's nominal
        top height; stacks are the world model's job (it knows what it placed where)."""
        fit = self._circle_on(pts_uv, 0.0, min_sectors=5)
        if fit is None:
            return None
        (x, y), r, conf = fit
        return (x, y), float(self.params["top_height"]), r, conf

    def _circle_on(self, pts_uv, z: float, min_sectors: int = 7, iterations: int = 400):
        """RANSAC circle of a plausible radius through the outline back-projected onto the plane z:
        ((x, y), r, confidence), or None. `min_sectors` of 12 (30 degree) sectors must hold inliers:
        a real rim covers an arc, a short edge (label text, a cable) does not."""
        import numpy as np

        step = max(1, len(pts_uv) // 300)
        uv = pts_uv[::step]
        if len(uv) < 12:
            return None
        (xy,) = self._to_planes(uv, (z,))
        rmin, rmax, tol = (float(self.params[k]) for k in ("min_radius", "max_radius", "inlier_tol"))
        rng = np.random.default_rng(0)
        best = None
        for _ in range(iterations):
            circle = _circle3(*xy[rng.choice(len(xy), 3, replace=False)])
            if circle is None or not rmin <= circle[1] <= rmax:
                continue
            c = np.array(circle[0])
            on = np.abs(np.hypot(*(xy - c).T) - circle[1]) < tol
            n = int(on.sum())
            if best is None or n > best[0]:
                best = (n, c, circle[1], on)
        if best is None:
            return None
        n, c, r, on = best
        ang = np.arctan2(xy[on, 1] - c[1], xy[on, 0] - c[0])
        sectors = len(np.unique(((ang + np.pi) / (2 * np.pi) * 12).astype(int) % 12))
        if sectors < min_sectors or n < 12:
            return None
        return (float(c[0]), float(c[1])), float(r), float(min(1.0, sectors / 8))


def _rays(camera, uv):
    """Camera centre and unit-less ray directions (robot frame) for many pixels at once."""
    import cv2
    import numpy as np

    K, dist = np.array(camera.K, float), np.array(camera.dist, float)
    R, t = np.array(camera.R, float), np.array(camera.t, float)
    n = cv2.undistortPoints(np.asarray(uv, float).reshape(-1, 1, 2), K, dist).reshape(-1, 2)
    rays = (R.T @ np.column_stack([n, np.ones(len(n))]).T).T
    return -R.T @ t, rays


def _circle3(a, b, c):
    import numpy as np

    ax, ay = a
    bx, by = b
    cx, cy = c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return (ux, uy), float(np.hypot(ax - ux, ay - uy))


def _cam_path(cfg: Any) -> str:
    raw = cfg.raw() if hasattr(cfg, "raw") else (cfg or {})
    return ((raw.get("cameras") or {}).get("calibration")) or "config/camera.json"


def _path(cfg: Any, p: str):
    return cfg.path(p) if hasattr(cfg, "path") else p
