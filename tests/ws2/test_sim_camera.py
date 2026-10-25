"""WS2.5 (pure Python, runs in `make check`): the sim's overhead camera pose and key-frame selection."""

from __future__ import annotations

import copy
import json
import math

import pytest

from skillsmith.config import Config, load_config
from skillsmith.hal.sim import DEFAULT_CAMERA, CameraPose, _frame_before, _phase_end, overhead_camera
from skillsmith.types import PrimitiveCall


def _cfg(calibration) -> Config:
    data = copy.deepcopy(load_config().raw())
    data["cameras"]["calibration"] = calibration
    return Config(data)


def test_look_at_puts_the_target_at_the_image_centre():
    cam = CameraPose.look_at((0.12, 0.0, 0.75), (0.18, 0.0, 0.0), (1.0, 0.0, 0.0), (640, 480), 60.0)
    u, v = cam.project((0.18, 0.0, 0.0))
    assert u == pytest.approx(320, abs=1e-6) and v == pytest.approx(240, abs=1e-6)
    assert cam.center() == pytest.approx((0.12, 0.0, 0.75), abs=1e-9)


def test_look_at_orientation_far_is_up_left_is_left():
    """+x (away from the base) is up in the image, +y (robot's left) is left: a top view."""
    cam = CameraPose.look_at((0.18, 0.0, 0.75), (0.18, 0.0, 0.0), (1.0, 0.0, 0.0), (640, 480), 60.0)
    u0, v0 = cam.project((0.18, 0.0, 0.0))
    _, v_far = cam.project((0.28, 0.0, 0.0))
    u_left, _ = cam.project((0.18, 0.10, 0.0))
    assert v_far < v0 and u_left < u0


def test_look_at_rotation_is_orthonormal_and_right_handed():
    cam = CameraPose.look_at((0.10, -0.05, 0.6), (0.2, 0.05, 0.0), (1.0, 0.0, 0.0), (640, 480), 55.0)
    R = cam.R
    for i in range(3):
        for j in range(3):
            assert sum(R[i][k] * R[j][k] for k in range(3)) == pytest.approx(float(i == j), abs=1e-9)
    x, y, z = R
    assert (x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]) == pytest.approx(z)


def test_vertical_fov_matches_the_focal_length():
    cam = CameraPose.look_at((0.18, 0.0, 1.0), (0.18, 0.0, 0.0), (1.0, 0.0, 0.0), (640, 480), 60.0)
    edge = math.tan(math.radians(30.0))  # a point at the top edge of a 60 deg vertical view, 1 m down
    _, v = cam.project((0.18 + edge, 0.0, 0.0))
    assert v == pytest.approx(0.0, abs=1e-6)


def test_default_camera_when_not_calibrated(tmp_path):
    cam = overhead_camera(_cfg(str(tmp_path / "missing.json")))
    assert cam.source == "default"
    assert cam.size == DEFAULT_CAMERA["size"]
    assert cam.center() == pytest.approx(DEFAULT_CAMERA["eye"])


def test_calibrated_camera_comes_from_camera_json(tmp_path):
    path = tmp_path / "camera.json"
    K = [[900.0, 0.0, 650.0], [0.0, 905.0, 350.0], [0.0, 0.0, 1.0]]
    R = [[0.0, -1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, -1.0]]
    path.write_text(json.dumps({"K": K, "dist": [0, 0, 0, 0, 0], "R": R, "t": [0.0, 0.18, 0.7],
                                "size": [1280, 720]}))
    cam = overhead_camera(_cfg(str(path)))
    assert cam.source == str(path) and cam.size == (1280, 720) and cam.K == K and cam.R == R
    assert cam.center() == pytest.approx((0.18, 0.0, 0.7))
    assert cam.project((0.18, 0.0, 0.0)) == pytest.approx((650.0, 350.0))


def _call(i: int, t: float) -> PrimitiveCall:
    return PrimitiveCall(index=i, name="move_to", args={}, t=t)


def test_phase_end_is_the_start_of_the_next_call():
    trace = [_call(0, 10.0), _call(1, 11.0), _call(2, 12.5)]
    assert _phase_end(trace, 0) == 11.0
    assert _phase_end(trace, 1) == 12.5
    assert _phase_end(trace, 2) == math.inf


def test_frame_before_picks_the_last_frame_up_to_the_time():
    marks = [(10.5, b"a"), (10.9, b"b"), (11.8, b"c")]
    assert _frame_before(marks, 11.0) == b"b"
    assert _frame_before(marks, math.inf) == b"c"
    assert _frame_before(marks, 10.0) is None
