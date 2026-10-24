"""WS3.7 wrist camera: config and geometry without cv2/numpy (runs in `make check` anywhere)."""

import math

import pytest

from skillsmith.config import Config
from skillsmith.perception.wrist import (
    WristConfig,
    clamp_offset,
    hue_ranges,
    pixel_to_tool,
    roi_box,
    tool_to_base,
    tool_yaw_deg,
)


def test_defaults_when_config_has_no_wrist_section():
    assert WristConfig.from_cfg(None) == WristConfig()
    assert WristConfig.from_cfg({"robot": {}}) == WristConfig()


def test_reads_wrist_section_from_config_and_detector_hues():
    cfg = Config({"wrist": {"px_per_m": 5000, "roi": [0.4, 0.5, 0.6, 0.9], "unknown_key": 1},
                  "detect": {"hsv": {"hues": {"pink": [[150, 165]]}}}})
    wc = WristConfig.from_cfg(cfg)
    assert wc.px_per_m == 5000 and wc.roi == (0.4, 0.5, 0.6, 0.9)
    assert hue_ranges("pink", wc) == [(150, 165)]
    assert WristConfig.from_cfg(wc) is wc


@pytest.mark.parametrize("bad", [
    {"roi": [0.6, 0.5, 0.4, 0.9]},
    {"px_per_m": 0},
    {"min_fill": 1.5},
    {"u_axis": "z"},
    {"u_axis": "+x", "v_axis": "-x"},
])
def test_rejects_bad_config(bad):
    with pytest.raises(ValueError):
        WristConfig.from_cfg({"wrist": bad})


def test_roi_box_in_pixels_and_never_empty():
    assert roi_box(640, 480, (0.25, 0.5, 0.75, 1.0)) == (160, 240, 480, 480)
    x0, y0, x1, y1 = roi_box(10, 10, (0.5, 0.5, 0.51, 0.51))
    assert x1 > x0 and y1 > y0


def test_hue_lookup_uses_last_colour_word():
    wc = WristConfig()
    assert hue_ranges("light blue", wc) == hue_ranges("blue", wc)
    assert hue_ranges("", wc) is None and hue_ranges(None, wc) is None and hue_ranges("chartreuse", wc) is None


def test_pixel_to_tool_default_axes():
    wc = WristConfig(px_per_m=4000)
    # image right = tool -y, image down = tool -x
    assert pixel_to_tool(40, 0, wc) == pytest.approx((0.0, -0.01))
    assert pixel_to_tool(0, -80, wc) == pytest.approx((0.02, 0.0))


def test_pixel_to_tool_scales_with_camera_distance():
    wc = WristConfig(px_per_m=4000, ref_distance=0.10)
    near = pixel_to_tool(40, 0, wc, distance=0.10)
    far = pixel_to_tool(40, 0, wc, distance=0.20)  # twice as far: each pixel covers twice the metres
    assert far[1] == pytest.approx(2 * near[1])


def test_custom_axes():
    wc = WristConfig(px_per_m=1000, u_axis="+x", v_axis="+y")
    assert pixel_to_tool(10, 20, wc) == pytest.approx((0.01, 0.02))


def test_clamp_keeps_direction():
    dx, dy, clamped = clamp_offset(0.03, 0.04, 0.02)
    assert clamped and math.hypot(dx, dy) == pytest.approx(0.02) and dx / dy == pytest.approx(0.75)
    assert clamp_offset(0.001, 0.0, 0.02) == (0.001, 0.0, False)


def test_tool_to_base_rotation_and_yaw():
    assert tool_to_base(0.01, 0.0, 0.0) == pytest.approx((0.01, 0.0))
    assert tool_to_base(0.01, 0.0, 90.0) == pytest.approx((0.0, 0.01))
    assert tool_yaw_deg([30, 0, 0, 0, 15, 50]) == pytest.approx(45.0)
    assert tool_yaw_deg([30, 0, 0, 0, 15, 50], WristConfig(roll_sign=-1.0)) == pytest.approx(15.0)
