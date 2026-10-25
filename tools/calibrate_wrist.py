"""Calibrate the wrist camera (field of view + pose on the gripper), or check it. OWNER: WS3c (WS3.7).

The model being fitted (see kinematics.wrist_camera_pose and perception.wrist.pixel_ray):
    camera pose in the robot frame = tip pose @ cam_in_tip       (MuJoCo camera axes: looks along -z, +y up)
    pixel (u, v) = centre + f * (x, -y) / -z,  f = (H / 2) / tan(fovy / 2),  centre = ((W - 1) / 2, (H - 1) / 2)
The ArUco markers on the mat (config/markers.json, same dictionary/corner order as tools/calibrate_camera.py)
give known robot-frame points; the arm's joints (robotd GET /state) give the tip pose by FK. Never moves the arm.

    # 1. capture: put the arm (by hand / teleop) where the wrist camera sees >= 3 markers, then
    python tools/calibrate_wrist.py --capture [--data wrist_calib]      # repeat for 2-3 more poses
    # 2. fit every capture in the folder; prints YAML to paste under `wrist:` in config/robot.yaml
    python tools/calibrate_wrist.py --fit [--data wrist_calib]
    # 3. check: wrist locate of each object the overhead camera sees, vs the overhead estimate
    python tools/calibrate_wrist.py --check [--data wrist_calib]        # also shows the fitted model if any

Only GET requests to robotd (/state, /frame/wrist); the overhead estimate comes from make_env('real').
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.request
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np

MIN_POINTS = 12           # 3 markers' corners: fewer cannot pin the focal length down
FOVY_GRID = np.arange(35.0, 121.0, 2.5)
JOINT_STILL_DEG = 0.2     # joints read before and after a frame must agree this well
MJ_FROM_CV = np.diag([1.0, -1.0, -1.0])  # OpenCV camera axes (x right, y down, z forward) <-> MuJoCo


# ---------------------------------------------------------------- pure geometry (numpy only)


def focal_px(fovy_deg: float, height: int) -> float:
    return (height / 2.0) / math.tan(math.radians(fovy_deg) / 2.0)


def fovy_from_focal(f: float, height: int) -> float:
    return math.degrees(2.0 * math.atan((height / 2.0) / f))


def rodrigues(r: Sequence[float]) -> np.ndarray:
    r = np.asarray(r, float)
    theta = float(np.linalg.norm(r))
    if theta < 1e-12:
        return np.eye(3)
    k = r / theta
    kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(theta) * kx + (1 - math.cos(theta)) * kx @ kx


def rot_angle_deg(Ra: np.ndarray, Rb: np.ndarray) -> float:
    c = (np.trace(Ra.T @ Rb) - 1.0) / 2.0
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def project(world_pts: np.ndarray, cam_pose: np.ndarray, f: float, size: Tuple[int, int]) -> np.ndarray:
    """Pixels of robot-frame points seen by a camera at `cam_pose` (4x4, MuJoCo axes); the inverse of pixel_ray."""
    w, h = size
    p = (np.linalg.inv(cam_pose) @ np.c_[world_pts, np.ones(len(world_pts))].T)[:3].T
    depth = -p[:, 2]
    return np.c_[(w - 1) / 2.0 + f * p[:, 0] / depth, (h - 1) / 2.0 - f * p[:, 1] / depth]


def _pose(R: np.ndarray, t: np.ndarray) -> np.ndarray:
    T = np.eye(4)
    T[:3, :3], T[:3, 3] = R, t
    return T


def _pnp_cam_pose(world_pts, img_pts, f, size):
    """Camera pose (4x4, MuJoCo axes, robot frame) from one view; (pose, rms px) or None."""
    import cv2

    w, h = size
    K = np.array([[f, 0, (w - 1) / 2.0], [0, f, (h - 1) / 2.0], [0, 0, 1.0]])
    obj, img = np.asarray(world_pts, float), np.asarray(img_pts, float)
    flags = cv2.SOLVEPNP_IPPE if np.ptp(obj[:, 2]) < 1e-9 else cv2.SOLVEPNP_ITERATIVE
    ok, rvec, tvec = cv2.solvePnP(obj, img, K, None, flags=flags)
    if not ok:
        return None
    rvec, tvec = cv2.solvePnPRefineLM(obj, img, K, None, rvec, tvec)
    R = cv2.Rodrigues(rvec)[0]
    pose = _pose(R.T @ MJ_FROM_CV, (-R.T @ tvec).reshape(3))
    err = project(obj, pose, f, size) - img
    return pose, float(np.sqrt(np.mean(np.sum(err ** 2, axis=1))))


def fit_wrist(views: List[Tuple[np.ndarray, np.ndarray, np.ndarray]], size: Tuple[int, int],
              fovy_init: Optional[float] = None, fix_fovy: bool = False, iterations: int = 50) -> dict:
    """Fit fovy and cam_in_tip to `views` = [(tip_pose 4x4, world_pts Nx3, img_pts Nx2), ...].

    Init: per-view solvePnP over a grid of fovy (or fovy_init), cam_in_tip = inv(tip) @ camera pose of the
    view with most points. Then Levenberg-Marquardt on all views jointly (7 params, 6 if fix_fovy).
    Returns fovy_deg, cam_in_tip, rms_px, per-view rms, per-view cam_in_tip spread, 1-sigma uncertainties."""
    views = [(np.asarray(T, float), np.asarray(W, float), np.asarray(Px, float)) for T, W, Px in views]
    n_pts = sum(len(W) for _, W, _ in views)
    if not views or n_pts < (4 if fix_fovy else MIN_POINTS):
        raise ValueError(f"{n_pts} marker corners in {len(views)} views; need >= {MIN_POINTS} "
                         "(3 markers in view, or more poses)")
    w, h = size
    if fix_fovy:
        grid = [61.0 if fovy_init is None else float(fovy_init)]
    else:
        grid = list(FOVY_GRID) + ([float(fovy_init)] if fovy_init is not None else [])
    best = None
    for fovy in grid:
        f = focal_px(fovy, h)
        sols = [_pnp_cam_pose(W, Px, f, size) if len(W) >= 4 else None for _, W, Px in views]
        if any(s is None for s in sols):
            continue
        score = sum(s[1] ** 2 * len(W) for s, (_, W, _) in zip(sols, views))
        if best is None or score < best[0]:
            best = (score, fovy, sols)
    if best is None:
        raise ValueError("solvePnP failed on every view (need >= 4 corners per view)")
    _, fovy0, sols = best
    main = max(range(len(views)), key=lambda i: len(views[i][1]))
    C0 = np.linalg.inv(views[main][0]) @ sols[main][0]
    per_view_c = [np.linalg.inv(T) @ s[0] for (T, _, _), s in zip(views, sols)]

    def unpack(p):
        C = C0 @ _pose(rodrigues(p[1:4]), p[4:7])
        return (fovy0 if fix_fovy else p[0]), C

    def residuals(p):
        fovy, C = unpack(p)
        if not 5.0 < fovy < 170.0:
            return np.full(2 * n_pts, 1e6)
        f = focal_px(fovy, h)
        return np.concatenate([(project(W, T @ C, f, size) - Px).ravel() for T, W, Px in views])

    p = np.zeros(7)
    p[0] = fovy0
    free = [i for i in range(7) if not (fix_fovy and i == 0)]
    lam, r = 1e-3, residuals(p)
    for _ in range(iterations):
        J = _jacobian(residuals, p, free)
        A, g = J.T @ J, J.T @ r
        improved = False
        for _ in range(10):
            step = np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-12), -g)
            q = p.copy()
            q[free] += step
            rq = residuals(q)
            if rq @ rq < r @ r:
                p, r, lam, improved = q, rq, max(lam / 10, 1e-9), True
                break
            lam *= 10
        if not improved or np.max(np.abs(step)) < 1e-10:
            break
    fovy, C = unpack(p)
    J = _jacobian(residuals, p, free)
    dof = max(1, len(r) - len(free))
    sigma2 = float(r @ r) / dof
    try:
        cov = np.linalg.inv(J.T @ J) * sigma2
        std = dict(zip([["fovy_deg", "rx", "ry", "rz", "tx", "ty", "tz"][i] for i in free],
                       np.sqrt(np.abs(np.diag(cov)))))
    except np.linalg.LinAlgError:
        std = {}
    f = focal_px(fovy, h)
    view_rms = [float(np.sqrt(np.mean(np.sum((project(W, T @ C, f, size) - Px) ** 2, axis=1))))
                for T, W, Px in views]
    spread_cm = max(float(np.linalg.norm(c[:3, 3] - C[:3, 3])) * 100 for c in per_view_c)
    spread_deg = max(rot_angle_deg(c[:3, :3], C[:3, :3]) for c in per_view_c)
    return {"fovy_deg": float(fovy), "cam_in_tip": C, "rms_px": float(np.sqrt(np.mean(r.reshape(-1, 2) ** 2) * 2)),
            "view_rms_px": view_rms, "n_views": len(views), "n_points": n_pts, "std": std,
            "view_spread_cm": spread_cm, "view_spread_deg": spread_deg, "fovy_init": float(fovy0)}


def _jacobian(fun, p, free, eps=1e-6):
    r0 = fun(p)
    J = np.empty((len(r0), len(free)))
    for k, i in enumerate(free):
        q = p.copy()
        q[i] += eps
        J[:, k] = (fun(q) - r0) / eps
    return J


def optical_roll_deg(C_new: np.ndarray, C_ref: np.ndarray) -> float:
    """Roll of the new camera about its optical axis relative to the reference (deg, image-plane rotation)."""
    R = C_ref[:3, :3].T @ C_new[:3, :3]  # new camera axes in the reference camera frame
    return math.degrees(math.atan2(R[1, 0], R[0, 0]))


def to_yaml(fovy_deg: float, cam_in_tip: np.ndarray) -> str:
    rows = "\n".join("    - [" + ", ".join(f"{v:.5f}" for v in row) + "]" for row in np.asarray(cam_in_tip))
    return f"  fovy_deg: {fovy_deg:.2f}\n  cam_in_tip:\n{rows}"


# ---------------------------------------------------------------- robot I/O (GET only)


def _get(url: str, timeout: float = 5.0) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
        return resp.read()


def read_joints(base: str) -> List[float]:
    return [float(v) for v in json.loads(_get(base + "/state"))["joints"][:5]]


def read_wrist(base: str):
    import cv2

    img = cv2.imdecode(np.frombuffer(_get(base + "/frame/wrist"), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit("robotd /frame/wrist did not return a JPEG")
    return img


def still_frame(base: str):
    """(frame, joints) with the arm still across the grab."""
    for _ in range(5):
        before = read_joints(base)
        img = read_wrist(base)
        after = read_joints(base)
        if max(abs(a - b) for a, b in zip(before, after)) <= JOINT_STILL_DEG:
            return img, [(a + b) / 2 for a, b in zip(before, after)]
        time.sleep(0.5)
    raise SystemExit("the arm kept moving during the capture; hold it still")


def default_cam_in_tip(cfg) -> np.ndarray:
    from skillsmith.kinematics import WRIST_CAM_IN_TIP

    wrist = cfg.get("wrist")
    c = wrist.get("cam_in_tip") if wrist is not None else None
    return np.asarray(c if c is not None else WRIST_CAM_IN_TIP, float)


def tip_poses(cfg, joint_sets):
    """Tip pose (4x4, robot frame, as wrist_camera_pose uses it) for each joint set."""
    from skillsmith.kinematics import make_kinematics

    kin = make_kinematics(cfg)
    inv_c = np.linalg.inv(default_cam_in_tip(cfg))
    try:
        return [np.asarray(kin.wrist_camera_pose(j), float) @ inv_c for j in joint_sets]
    finally:
        close = getattr(kin, "close", None)
        if callable(close):
            close()


def marker_table(cfg, marker_size: Optional[float]):
    from skillsmith.perception.geometry import MARKER_SIZE

    markers = {int(k): tuple(v) for k, v in json.loads(cfg.path("config/markers.json").read_text()).items()}
    if marker_size is None:
        try:
            marker_size = float(json.loads(cfg.path(cfg.cameras.calibration).read_text()).get("marker_size"))
        except (OSError, TypeError, ValueError, AttributeError):
            marker_size = MARKER_SIZE
    return markers, marker_size


def load_views(cfg, data: Path, marker_size: Optional[float]):
    """[(tip_pose, world_pts, img_pts, name)], image size, from the captures in `data`."""
    import cv2

    from skillsmith.perception.geometry import detect_markers, marker_corners_world

    markers, size_m = marker_table(cfg, marker_size)
    caps = sorted(data.glob("*.json"))
    joints = [json.loads(c.read_text())["joints"] for c in caps]
    tips = tip_poses(cfg, joints) if caps else []
    views, size = [], None
    for cap, tip in zip(caps, tips):
        img = cv2.imread(str(cap.with_suffix(".jpg")))
        if img is None:
            continue
        size = (img.shape[1], img.shape[0])
        found = detect_markers(img)
        ids = sorted(set(found) & set(markers))
        extra = sorted(set(found) - set(markers))
        print(f"{cap.stem}: markers {ids or 'none'}" + (f" (ignored, not in markers.json: {extra})" if extra else ""))
        if ids:
            W = np.array([p for i in ids for p in marker_corners_world(*markers[i], size_m)], float)
            Px = np.array([p for i in ids for p in found[i]], float)
            views.append((tip, W, Px, cap.stem))
    return views, size


def fit_from_data(cfg, args):
    views, size = load_views(cfg, Path(args.data), args.marker_size)
    if not views:
        raise SystemExit(f"no captures with markers in {args.data} (run --capture with markers in the wrist view)")
    fovy_cfg = float((cfg.get("wrist") or {}).get("fovy_deg", 61.0))
    res = fit_wrist([v[:3] for v in views], size, fovy_init=fovy_cfg, fix_fovy=args.fix_fovy)
    return res, views, size


def report_fit(cfg, res, views):
    ref = default_cam_in_tip(cfg)
    C = res["cam_in_tip"]
    print(f"\n{res['n_points']} corners in {res['n_views']} views; reprojection {res['rms_px']:.2f} px rms "
          f"(per view: {', '.join(f'{v[3]} {e:.2f}' for v, e in zip(views, res['view_rms_px']))})")
    std = res["std"]
    if std:
        print("1-sigma: " + ", ".join(f"{k} {v * (100 if k[0] == 't' else 1):.2f}{'cm' if k[0] == 't' else ''}"
                                      for k, v in std.items()))
    print(f"vs current model: camera moved {np.linalg.norm(C[:3, 3] - ref[:3, 3]) * 100:.1f} cm, "
          f"rotated {rot_angle_deg(ref[:3, :3], C[:3, :3]):.1f} deg "
          f"(image-plane roll {optical_roll_deg(C, ref):+.1f} deg)")
    if res["n_views"] > 1:
        print(f"per-view solutions disagree by up to {res['view_spread_cm']:.1f} cm / {res['view_spread_deg']:.1f} deg")
    else:
        print("WARNING: one pose only: FK and joint-offset errors are absorbed into cam_in_tip. Capture 2-3 more "
              "poses (different heights, wrist_flex and wrist_roll, markers in view) and refit.")
    print("\n# paste under `wrist:` in config/robot.yaml")
    print(to_yaml(res["fovy_deg"], C))


# ---------------------------------------------------------------- check vs overhead


def overhead_objects(cfg, wait_s: float = 6.0):
    from skillsmith.hal.base import make_env

    env = make_env("real", cfg)
    try:
        deadline = time.time() + wait_s
        while True:
            objs = [o for o in env.perception.observe().objects if o.visible]
            if objs or time.time() > deadline:
                return objs
            time.sleep(0.5)
    finally:
        env.close()


def run_check(cfg, args) -> int:
    from skillsmith.perception import wrist

    base = cfg.robot.robotd_url.rstrip("/")
    objs = overhead_objects(cfg)
    if not objs:
        print("the overhead camera sees no objects")
        return 1
    img, joints = still_frame(base)
    size = (img.shape[1], img.shape[0])
    tip = tip_poses(cfg, [joints])[0]
    models = [("config", float((cfg.get("wrist") or {}).get("fovy_deg", 61.0)), default_cam_in_tip(cfg))]
    if args.data and any(Path(args.data).glob("*.json")):
        try:
            res, _, _ = fit_from_data(cfg, args)
            models.append(("fitted", res["fovy_deg"], res["cam_in_tip"]))
        except (SystemExit, ValueError) as e:
            print(f"(no fitted model: {e})")
    raw = cfg.raw()
    if args.min_s is not None:
        raw = dict(raw, wrist=dict(raw.get("wrist") or {}, min_s=args.min_s))
    print(f"joints {[round(j, 1) for j in joints]}")
    worst = 0.0
    for o in objs:
        print(f"{o.id}: overhead ({o.x:.3f}, {o.y:.3f}), top z={o.top:.3f}")
        for name, fovy, C in models:
            cfgm = dict(raw, wrist=dict(raw.get("wrist") or {}, fovy_deg=fovy))
            pose = tip @ C
            u, v = project(np.array([[o.x, o.y, o.top]]), pose, focal_px(fovy, size[1]), size)[0]
            r = wrist.locate_on_plane(img, o.color or None, pose, o.top, cfgm)
            line = f"  {name:7s} overhead top projects to ({u:.0f}, {v:.0f}) px; "
            if r.found:
                err = math.hypot(r.x - o.x, r.y - o.y)
                worst = max(worst, err) if name == models[-1][0] else worst
                line += (f"blob ({r.u:.0f}, {r.v:.0f}) -> ({r.x:.3f}, {r.y:.3f}), {err * 100:.1f} cm from overhead "
                         f"(confidence {r.confidence:.2f})")
            else:
                line += r.reason
            print(line)
    return 0 if worst <= 0.01 else 1


def run_capture(cfg, args) -> int:
    import cv2

    base = cfg.robot.robotd_url.rstrip("/")
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for k in range(args.frames):
        img, joints = still_frame(base)
        name = data / f"{stamp}_{k}"
        cv2.imwrite(str(name.with_suffix(".jpg")), img)
        name.with_suffix(".json").write_text(json.dumps({"joints": joints, "t": time.time()}))
        time.sleep(0.2)
    print(f"saved {args.frames} frames at joints {[round(j, 1) for j in joints]} to {data}")
    load_views(cfg, data, args.marker_size)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture", action="store_true", help="save wrist frames + joints at the current pose")
    mode.add_argument("--fit", action="store_true", help="fit fovy + cam_in_tip to every capture in --data")
    mode.add_argument("--check", action="store_true", help="wrist locate vs the overhead estimate")
    p.add_argument("--data", default="wrist_calib", help="capture folder (default: wrist_calib)")
    p.add_argument("--frames", type=int, default=3, help="frames per capture")
    p.add_argument("--marker-size", type=float, help="black square edge, m (default: camera.json's)")
    p.add_argument("--fix-fovy", action="store_true", help="keep the config fovy_deg, fit the pose only")
    p.add_argument("--min-s", type=int, help="--check: override wrist.min_s (washed-out colours)")
    args = p.parse_args(argv)
    from skillsmith.config import load_config

    cfg = load_config()
    if args.capture:
        return run_capture(cfg, args)
    if args.check:
        return run_check(cfg, args)
    try:
        res, views, _ = fit_from_data(cfg, args)
    except ValueError as e:
        print(f"fit failed: {e}", file=sys.stderr)
        return 1
    report_fit(cfg, res, views)
    return 0


if __name__ == "__main__":
    sys.exit(main())
