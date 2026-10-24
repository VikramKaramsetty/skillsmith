"""WS3.8 annotation planning and crop windows: pure Python, no cv2/numpy needed."""

import pytest

from skillsmith.perception.annotate import Overlays, crop_window, object_bbox, plan_overlays
from skillsmith.perception.color import Detection
from skillsmith.types import Scene, SceneObject


class ScaleCamera:
    """Stub projection: straight down, 1 px per mm, origin at pixel (100, 100); z is ignored."""

    def project(self, x, y, z):
        return (100 + 1000 * x, 100 + 1000 * y)


def cup(id_, label, color, x, y, visible=True):
    return SceneObject(id_, label, color, "cup", x, y, 0.0, 0.08, 0.06, visible=visible)


SCENE = Scene(
    (cup("red_cup_0", "red cup", "red", 0.1, 0.1), cup("blue_cup_0", "blue cup", "blue", 0.2, 0.1, False)), 0.0, "fake"
)


def test_object_bbox_covers_the_projected_footprint():
    assert object_bbox(ScaleCamera(), SCENE.objects[0]) == (170, 170, 230, 230)


def test_plan_boxes_every_scene_object_and_marks_hidden_ones():
    plan = plan_overlays(SCENE, ScaleCamera(), None)
    assert [(b.label, b.color, b.dashed) for b in plan.boxes] == [
        ("red cup", "red", False),
        ("blue cup (hidden)", "blue", True),
    ]


def test_detections_replace_the_projected_box_for_their_label():
    det = Detection("red cup", "red", "cup", (1, 2, 3, 4), (2.0, 2.0), 0.91)
    plan = plan_overlays(SCENE, ScaleCamera(), {"detections": [det]})
    assert plan.boxes[0].bbox == (1, 2, 3, 4) and plan.boxes[0].confidence == 0.91
    assert [b.label for b in plan.boxes] == ["red cup", "blue cup (hidden)"]


def test_robot_frame_points_project_and_error_is_measured_in_cm():
    ov = Overlays(target=(0.2, 0.1, 0.16), gripper=(0.21, 0.1, 0.2), actual=(0.17, 0.14, 0.08))
    plan = plan_overlays(SCENE, ScaleCamera(), ov)
    assert plan.target == pytest.approx((300.0, 200.0))
    assert plan.gripper == pytest.approx((310.0, 200.0))
    start, end, label = plan.arrow
    assert start == pytest.approx((300.0, 200.0)) and end == pytest.approx((270.0, 240.0))
    assert label == "5.0 cm"


def test_pixel_points_need_no_camera_and_explicit_error_wins():
    plan = plan_overlays(None, None, {"target_uv": (10, 20), "actual_uv": (30, 20), "error_cm": 3.2, "title": "carry"})
    assert plan.boxes == [] and plan.gripper is None
    assert plan.arrow == ((10, 20), (30, 20), "3.2 cm") and plan.title == "carry"


def test_no_arrow_without_both_ends():
    assert plan_overlays(None, None, {"target_uv": (10, 20)}).arrow is None


def test_unknown_overlay_keys_are_rejected():
    with pytest.raises(ValueError, match="crosshair"):
        Overlays.of({"crosshair": (1, 2)})


def test_crop_window_is_padded_square_around_the_union():
    assert crop_window([(100, 100, 139, 119), (160, 100, 199, 119)], (640, 480), pad=0.25) == (75, 35, 224, 184)


def test_crop_window_respects_min_side_and_image_edges():
    assert crop_window([(0, 0, 9, 9)], (640, 480), pad=0.0, min_side=96) == (0, 0, 95, 95)
    assert crop_window([(630, 470, 639, 479)], (640, 480), pad=0.0, min_side=96) == (544, 384, 639, 479)
    left, top, right, bottom = crop_window([(0, 0, 639, 479)], (640, 480))
    assert right - left + 1 == 480 and bottom - top + 1 == 480 and top == 0


def test_crop_window_needs_a_box():
    with pytest.raises(ValueError):
        crop_window([], (640, 480))
