"""Model-free cup detector (perception/diffdetect.py): synthetic renders through a calibrated camera."""

import math

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from skillsmith.perception.diffdetect import DiffDetector  # noqa: E402
from skillsmith.perception.geometry import CameraModel  # noqa: E402

CFG = {"workspace": {"x": [0.08, 0.34], "y": [-0.20, 0.20]}, "detect": {"diff": {"top_height": 0.08}}}
W, H = 960, 720


def camera() -> CameraModel:
    """Looking down from (0.40, 0, 0.45), tilted 25 degrees back toward the robot, like the iPhone mount."""
    a = math.radians(25)
    down = np.array([[0, -1, 0], [-1, 0, 0], [0, 0, -1]], float)  # camera x = -y, y = -x, z = -z (world)
    tilt = np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])
    R = tilt @ down
    C = np.array([0.40, 0.0, 0.45])
    K = [[700.0, 0, W / 2], [0, 700.0, H / 2], [0, 0, 1]]
    return CameraModel(K=K, dist=[0.0] * 5, R=R.tolist(), t=(-R @ C).tolist())


def table() -> np.ndarray:
    img = np.full((H, W, 3), 235, np.uint8)
    cv2.line(img, (0, 300), (W, 310), (200, 200, 200), 2)  # printed grid lines stay in the background
    return img


def draw_cup(img, cam, x, y, base_r=0.035, top_r=0.027, height=0.08, color=(60, 120, 200)):
    ring = [cam.project(x + r * math.cos(t), y + r * math.sin(t), z)
            for r, z in ((base_r, 0.0), (top_r, height)) for t in np.linspace(0, 2 * math.pi, 90)]
    hull = cv2.convexHull(np.array(ring, np.float32)).astype(np.int32)
    cv2.fillPoly(img, [hull], color)
    top = np.array([cam.project(x + top_r * math.cos(t), y + top_r * math.sin(t), height)
                    for t in np.linspace(0, 2 * math.pi, 90)], np.int32)
    cv2.fillPoly(img, [top], (238, 238, 238))  # a white top on a white mat: barely differs
    return img


def detector(cam=None):
    cam = cam or camera()
    return DiffDetector(CFG, ["white cup"], camera=cam, background=table()), cam


@pytest.mark.parametrize("x, y", [(0.22, 0.0), (0.15, -0.10), (0.28, 0.10)])
def test_finds_the_cup_from_its_base_rim_within_5_mm(x, y):
    det, cam = detector()
    [d] = det.detect(draw_cup(table(), cam, x, y))
    gx, gy = cam.pixel_to_table(*d.point_uv, 0.08)
    assert math.hypot(gx - x, gy - y) < 0.005  # the real target is 1 cm; the rig measured 0.27 cm mean
    assert (d.label, d.kind, d.source) == ("white cup", "cup", "diff")


def test_the_empty_table_has_no_cups():
    det, _ = detector()
    assert det.detect(table()) == []


def test_short_edges_and_off_mat_blobs_are_not_cups():
    det, cam = detector()
    img = table()
    cv2.rectangle(img, (300, 500), (420, 512), (40, 40, 40), -1)  # a label / cable: no rim arc
    draw_cup(img, cam, 0.22, 0.45)  # a cup-shaped thing well off the workspace
    assert det.detect(img) == []
