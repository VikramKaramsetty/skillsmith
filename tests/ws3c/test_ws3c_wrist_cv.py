"""WS3.7 wrist camera on synthetic frames (heavy job: needs cv2/numpy)."""

import pytest

from skillsmith.perception.wrist import (
    WristConfig,
    align_offset,
    check_holding,
    fine_offset,
    holding_check,
)

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

W, H = 640, 480
BLUE, RED, GREY, FINGER = (200, 60, 20), (30, 30, 210), (130, 128, 125), (50, 50, 50)
CFG = {"wrist": {"px_per_m": 4000, "ref_distance": 0.10, "max_offset": 0.02}}


def table():
    """A grey table with the two dark gripper fingers at the bottom edge."""
    img = np.full((H, W, 3), GREY, np.uint8)
    img[300:, 180:230] = FINGER
    img[300:, 410:460] = FINGER
    return img


def with_block(img, cx, cy, half, colour):
    img[max(cy - half, 0):cy + half, max(cx - half, 0):cx + half] = colour
    return img


# ---------------------------------------------------------------- holding


def test_object_between_fingers_is_held():
    frame = with_block(table(), 320, 330, 80, BLUE)
    result = check_holding(frame, "blue", CFG)
    assert result.holding and result.fill > 0.5 and result.confidence > 0.9
    assert "holding blue" in result.reason


def test_empty_gripper_is_not_held():
    result = check_holding(table(), "blue", CFG)
    assert not result.holding and result.fill == 0.0 and result.confidence == 1.0


def test_wrong_colour_is_not_the_object_but_counts_when_colour_unknown():
    frame = with_block(table(), 320, 330, 80, RED)
    assert not check_holding(frame, "blue", CFG).holding
    unknown = check_holding(frame, None, CFG)
    assert unknown.holding and unknown.confidence < 0.8  # less sure without a colour


def test_object_slipped_to_the_edge_is_not_held():
    frame = with_block(table(), 320, 450, 12, BLUE)  # a sliver at the bottom of the gap
    result = check_holding(frame, "blue", CFG)
    assert not result.holding and 0.0 < result.fill < 0.25


def test_dark_frame_cannot_judge():
    result = check_holding(np.full((H, W, 3), 5, np.uint8), "blue", CFG)
    assert not result.holding and result.confidence == 0.0 and "too dark" in result.reason


def test_holding_check_contract_shape():
    held, reason = holding_check(with_block(table(), 320, 330, 80, BLUE), "blue", CFG)
    assert held is True and "confidence" in reason


def test_bad_frame_shape_raises():
    with pytest.raises(ValueError):
        check_holding(np.zeros((H, W), np.uint8), "blue", CFG)


# ---------------------------------------------------------------- fine alignment


def test_centred_object_needs_no_correction():
    result = fine_offset(with_block(table(), 320, 240, 30, BLUE), "blue", CFG)
    assert result.found and abs(result.dx) < 0.0005 and abs(result.dy) < 0.0005 and result.confidence == 1.0


def test_object_right_of_centre_moves_tool_minus_y():
    result = fine_offset(with_block(table(), 360, 240, 30, BLUE), "blue", CFG)  # 40 px right = 1 cm
    assert result.found and result.dy == pytest.approx(-0.01, abs=0.0005) and abs(result.dx) < 0.0005
    assert "px from centre" in result.reason


def test_object_above_centre_moves_tool_plus_x():
    result = fine_offset(with_block(table(), 320, 180, 30, BLUE), "blue", CFG)  # 60 px up = 1.5 cm
    assert result.dx == pytest.approx(0.015, abs=0.0005) and abs(result.dy) < 0.0005


def test_distance_rescales_metres():
    frame = with_block(table(), 360, 240, 30, BLUE)
    assert fine_offset(frame, "blue", CFG, distance=0.05).dy == pytest.approx(-0.005, abs=0.0005)


def test_picks_the_object_colour_not_a_distractor():
    frame = with_block(with_block(table(), 360, 240, 30, BLUE), 200, 120, 40, RED)
    assert fine_offset(frame, "blue", CFG).dy == pytest.approx(-0.01, abs=0.0005)


def test_no_object_gives_zero_and_not_found():
    result = fine_offset(table(), "blue", CFG)
    assert not result.found and (result.dx, result.dy) == (0.0, 0.0) and "no blue" in result.reason
    assert align_offset(table(), "blue", CFG) == (0.0, 0.0)


def test_large_offset_is_clamped_and_flagged():
    result = fine_offset(with_block(table(), 600, 240, 25, BLUE), "blue", CFG)  # 280 px = 7 cm
    assert abs(result.dy) == pytest.approx(0.02, abs=1e-4) and result.confidence <= 0.5
    assert "clamped" in result.reason


def test_partial_object_at_the_edge_is_less_confident():
    result = fine_offset(with_block(table(), 5, 240, 30, BLUE), "blue", {"wrist": {"max_offset": 1.0}})
    assert result.found and result.confidence <= 0.5 and "edge" in result.reason


def test_tiny_speck_is_ignored():
    assert not fine_offset(with_block(table(), 360, 240, 4, BLUE), "blue", CFG).found


def test_align_offset_rotates_into_base_frame():
    frame = with_block(table(), 360, 240, 30, BLUE)  # tool (0, -0.01)
    assert align_offset(frame, "blue", CFG, yaw_deg=0.0) == pytest.approx((0.0, -0.01), abs=0.0005)
    assert align_offset(frame, "blue", CFG, yaw_deg=90.0) == pytest.approx((0.01, 0.0), abs=0.0005)


def test_custom_target_pixel():
    cfg = {"wrist": {"center": [0.5625, 0.5], "px_per_m": 4000}}  # tip projects 40 px right of centre
    result = fine_offset(with_block(table(), 360, 240, 30, BLUE), "blue", cfg)
    assert abs(result.dy) < 0.0005
    assert WristConfig.from_cfg(cfg).center == (0.5625, 0.5)
