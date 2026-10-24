"""WS3.8 drawing and crops on track A's fixture frames (heavy job: needs cv2/numpy)."""

import json
from pathlib import Path

import pytest

from skillsmith.perception.annotate import annotate_keyframe, crop_pair, side_by_side, write_image
from skillsmith.types import Scene, SceneObject

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

FIXTURES = Path(__file__).resolve().parents[1] / "ws3" / "fixtures"
TRUTH = json.loads((FIXTURES / "truth.json").read_text())
SCENES = {f["file"]: f for f in TRUTH["frames"]}


class PinholeCamera:
    """Stub projection from the fixture's ground-truth pose (we don't import track A's
    calibration): X_cam = R @ X_robot + t, then K."""

    def __init__(self, cam):
        self.K, self.R, self.t = (np.array(cam[k], float) for k in ("K", "R", "t"))

    def project(self, x, y, z):
        p = self.K @ (self.R @ np.array([x, y, z]) + self.t)
        return (float(p[0] / p[2]), float(p[1] / p[2]))


CAM = PinholeCamera(TRUTH["camera"])


def scene(name):
    keys = ("id", "label", "color", "kind", "x", "y", "z", "height", "width")
    objs = tuple(SceneObject(*(o[k] for k in keys)) for o in SCENES[name]["objects"])
    return Scene(objs, SCENES[name]["t"], "camera:overhead")


def frame(name):
    return cv2.imread(str(FIXTURES / name))


def test_annotate_draws_everything_without_touching_the_input():
    img = frame("apart.jpg")
    original = img.copy()
    red = scene("apart.jpg").require("red cup")
    overlays = {
        "target": (0.22, 0.0, 0.16),
        "gripper": (0.22, 0.02, 0.2),
        "actual": (red.x, red.y, red.top),
        "title": "place · MISSED_TARGET",
    }
    out = annotate_keyframe(img, scene("apart.jpg"), CAM, overlays)
    assert np.array_equal(img, original)
    assert out.shape == img.shape and not np.array_equal(out, img)
    # the target crosshair is yellow, the gripper dot magenta, the arrow red
    tu, tv = (round(c) for c in CAM.project(0.22, 0.0, 0.16))
    assert tuple(out[tv, tu + 10]) == (0, 255, 255)
    gu, gv = (round(c) for c in CAM.project(0.22, 0.02, 0.2))
    assert tuple(out[gv, gu]) == (255, 0, 255)
    reds = (out[:, :, 2] > 200) & (out[:, :, 1] < 30) & (out[:, :, 0] < 30)
    assert reds.sum() > 50


def test_annotate_works_from_pixels_alone():
    img = frame("apart.jpg")
    out = annotate_keyframe(img, None, None, {"target_uv": (100, 100), "actual_uv": (160, 140), "error_cm": 4.0})
    assert tuple(out[100, 110]) == (0, 255, 255)


def test_scene_boxes_surround_each_objects_top_pixel():
    img = frame("edges.jpg")
    out = annotate_keyframe(img, scene("edges.jpg"), CAM, None)
    changed = np.any(out != img, axis=2)
    for o in SCENES["edges.jpg"]["objects"]:
        u, v = (round(c) for c in o["top_uv"])
        assert not changed[v, u], f"box drawn over the centre of the {o['label']}"
        assert changed[:v, u].any() and changed[v:, u].any(), f"no box around the {o['label']}"


def test_crop_pair_is_one_window_around_the_named_objects():
    before, after = frame("apart.jpg"), frame("stacked.jpg")
    scenes = [scene("apart.jpg"), scene("stacked.jpg")]
    b, a = crop_pair(before, after, scenes, ["blue cup", "red cup"], CAM)
    assert b.shape == a.shape and b.shape[0] == b.shape[1] and b.shape[0] < 480
    # both cups (before) are inside the crop: find the window by template matching
    assert inside(before, b, "blue cup") and inside(before, b, "red cup")
    # a crop of one cup leaves the other, not named, out
    b, _ = crop_pair(before, after, scenes, ["red cup"], CAM)
    assert inside(before, b, "red cup") and not inside(before, b, "blue cup")


def inside(full, crop, label):
    """Is the object's top pixel (apart.jpg truth) inside the window `crop` was cut from?"""
    _, _, (x0, y0), _ = cv2.minMaxLoc(cv2.matchTemplate(full, crop, cv2.TM_SQDIFF))
    u, v = next(o["top_uv"] for o in SCENES["apart.jpg"]["objects"] if o["label"] == label)
    return x0 <= u < x0 + crop.shape[1] and y0 <= v < y0 + crop.shape[0]


def test_crop_pair_from_detection_boxes_and_errors():
    img = frame("apart.jpg")
    b, a = crop_pair(img, img, None, ["red cup"], boxes={"red cup": (230, 200, 260, 250)}, pad=0.0, min_side=10)
    assert b.shape[:2] == (51, 51) and np.array_equal(b, a)
    with pytest.raises(ValueError, match="could be located"):
        crop_pair(img, img, scene("apart.jpg"), ["purple ball"], CAM)
    with pytest.raises(ValueError, match="640x480"):
        crop_pair(img, img[:100], None, ["red cup"], boxes={"red cup": (0, 0, 9, 9)})


def test_side_by_side_and_write(tmp_path):
    img = frame("apart.jpg")
    b, a = crop_pair(img, frame("stacked.jpg"), scene("apart.jpg"), ["red cup"], CAM)
    pair = side_by_side(b, a)
    assert pair.shape[0] == 240 and pair.shape[1] > 480
    path = write_image(tmp_path / "attempt_1" / "carry.png", pair)
    assert cv2.imread(path).shape == pair.shape
