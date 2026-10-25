"""tools/calibrate_wrist.py: fit fovy + cam_in_tip from synthetic marker views (no hardware, no placo)."""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

_spec = importlib.util.spec_from_file_location(
    "calibrate_wrist", Path(__file__).resolve().parents[2] / "tools" / "calibrate_wrist.py")
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)

from skillsmith.perception.wrist import pixel_ray  # noqa: E402

SIZE = (640, 480)
TRUE_FOVY = 72.0
# a camera rolled 90 deg about its optical axis vs the MJCF mount, a few cm off
TRUE_C = cw._pose(cw.rodrigues([0.1, -0.05, math.pi / 2]), np.array([0.02, 0.06, -0.03]))
MARKERS = [(0.0961, -0.1813), (0.2886, -0.1372), (0.2926, 0.1377), (0.1004, 0.1782)]


def _corners(size=0.04):
    h = size / 2
    return np.array([(x + dx, y + dy, 0.0) for x, y in MARKERS for dx, dy in ((h, h), (h, -h), (-h, -h), (-h, h))])


def _tip_looking_at(eye, target=(0.2, 0.0, 0.0)):
    """Tip pose such that TRUE_C puts the camera at `eye` looking at `target` (MuJoCo axes)."""
    eye, target = np.asarray(eye, float), np.asarray(target, float)
    back = eye - target
    back /= np.linalg.norm(back)
    right = np.cross([0, 0, 1.0], back)
    right /= np.linalg.norm(right)
    cam = cw._pose(np.c_[right, np.cross(back, right), back], eye)
    return cam @ np.linalg.inv(TRUE_C)


def _views(eyes, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    f = cw.focal_px(TRUE_FOVY, SIZE[1])
    out = []
    for eye in eyes:
        tip = _tip_looking_at(eye)
        W = _corners()
        Px = cw.project(W, tip @ TRUE_C, f, SIZE) + rng.normal(0, noise, (len(W), 2))
        assert (Px[:, 0] > 0).all() and (Px[:, 0] < SIZE[0]).all() and (Px[:, 1] > 0).all() and (Px[:, 1] < SIZE[1]).all()
        out.append((tip, W, Px))
    return out


def test_project_inverts_pixel_ray():
    f = cw.focal_px(61.0, SIZE[1])
    u, v = cw.project(np.array([[0.03, -0.02, -0.2]]), np.eye(4), f, SIZE)[0]
    ray = pixel_ray(u, v, SIZE[0], SIZE[1], 61.0)
    assert ray[0] * 0.2 == pytest.approx(0.03, abs=1e-9) and ray[1] * 0.2 == pytest.approx(-0.02, abs=1e-9)


def test_recovers_fovy_and_cam_in_tip_from_several_poses():
    res = cw.fit_wrist(_views([(0.2, -0.35, 0.35), (0.45, 0.1, 0.3), (0.05, 0.3, 0.4)], noise=0.3), SIZE,
                       fovy_init=61.0)
    assert res["rms_px"] < 0.6
    assert res["fovy_deg"] == pytest.approx(TRUE_FOVY, abs=0.5)
    assert np.linalg.norm(res["cam_in_tip"][:3, 3] - TRUE_C[:3, 3]) < 0.003
    assert cw.rot_angle_deg(res["cam_in_tip"][:3, :3], TRUE_C[:3, :3]) < 0.5
    assert cw.optical_roll_deg(res["cam_in_tip"], np.eye(4)) == pytest.approx(
        cw.optical_roll_deg(TRUE_C, np.eye(4)), abs=0.5)


def test_single_noise_free_pose_is_exact():
    res = cw.fit_wrist(_views([(0.2, -0.35, 0.35)]), SIZE)
    assert res["n_views"] == 1 and res["rms_px"] < 1e-3
    assert res["fovy_deg"] == pytest.approx(TRUE_FOVY, abs=0.05)


def test_too_few_corners_is_refused():
    tip, W, Px = _views([(0.2, -0.35, 0.35)])[0]
    with pytest.raises(ValueError, match="need"):
        cw.fit_wrist([(tip, W[:8], Px[:8])], SIZE)


def test_yaml_pastes_under_wrist():
    text = cw.to_yaml(61.234, np.eye(4))
    assert text.splitlines()[0] == "  fovy_deg: 61.23"
    assert text.splitlines()[2] == "    - [1.00000, 0.00000, 0.00000, 0.00000]"
