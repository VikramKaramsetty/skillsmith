"""perception.wrist.locate_on_plane: wrist-camera ray through the colour blob meets the object's top plane."""

from __future__ import annotations

import math

import pytest

from skillsmith.perception.wrist import locate_on_plane

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

W, H, FOVY = 640, 480, 61.0
F = (H / 2) / math.tan(math.radians(FOVY) / 2)
PINK_BGR = (180, 60, 255)  # hue ~165 in OpenCV, like the rig's painted cup top
CFG = {"wrist": {"fovy_deg": FOVY}, "detect": {"hsv": {"hues": {"pink": [[150, 169]]}}}}


def _down_camera(x, y, z):
    """Camera at (x, y, z) looking straight down (MuJoCo axes: -z forward, +y up = robot +y)."""
    pose = np.eye(4)
    pose[:3, 3] = (x, y, z)
    return pose


def _frame(u, v, radius=30):
    img = np.full((H, W, 3), 90, np.uint8)
    cv2.circle(img, (int(u), int(v)), radius, PINK_BGR, -1)
    return img


def test_blob_off_centre_maps_to_table_offset():
    du, dv = 64, -40  # right of and above the image centre
    res = locate_on_plane(_frame((W - 1) / 2 + du, (H - 1) / 2 + dv), "pink", _down_camera(0.25, 0.0, 0.18), 0.095, CFG)
    depth = 0.18 - 0.095
    assert res.found and res.confidence > 0.9
    assert res.x == pytest.approx(0.25 + du / F * depth, abs=0.001)   # image right = camera +x = robot +x here
    assert res.y == pytest.approx(0.0 - dv / F * depth, abs=0.001)    # image up = camera +y = robot +y here


def test_no_blob_or_camera_below_the_plane_is_not_found():
    assert not locate_on_plane(np.full((H, W, 3), 90, np.uint8), "pink", _down_camera(0.25, 0, 0.18), 0.095, CFG).found
    low = locate_on_plane(_frame(320, 240), "pink", _down_camera(0.25, 0, 0.05), 0.095, CFG)
    assert not low.found and "never reaches" in low.reason


def test_approach_side_moves_the_grasp_to_the_wrist_fix_and_ignores_wild_ones():
    from skillsmith.config import load_config
    from skillsmith.events import NullBus
    from skillsmith.hal.base import make_env
    from skillsmith.skills.runtime import run_skill
    from skillsmith.types import Scene, SceneObject

    cfg = load_config()
    cfg._data = {**cfg._data, "wrist": {**(cfg._data.get("wrist") or {}), "fovy_deg": FOVY, "locate": True}}
    cup = SceneObject("pink_cup_0", "pink cup", "pink", "cup", 0.26, 0.0, 0.0, 0.0953, 0.0762, top_width=0.0508)
    src = ('SKILL = {"name": "t", "params": {"shift": "float"}}\n\n'
           'def run(robot, scene, skills, shift):\n'
           '    fix = robot.approach_side("pink cup")\n'
           '    robot.note(f"fix={fix} at={robot.position()}")\n')

    def run(shift):
        env = make_env("fake", cfg, Scene((cup,), 0.0, "test"))
        # a camera straight above the cup, WRIST_DEPTH over its top (the real one is FK-carried)
        env.kin.wrist_camera_pose = lambda q: _down_camera(cup.x, cup.y, cup.top + WRIST_DEPTH)
        env.perception.wrist_frame = lambda: _frame((W - 1) / 2 + shift * F / WRIST_DEPTH, (H - 1) / 2)
        res = run_skill(src, env, {"shift": shift}, cfg, NullBus(), skill="t")
        assert res.ok, res.error
        return res.trace[0].result, res.trace[-1].ee_before

    from skillsmith.primitives import SIDE_HOVER, SWING_CLEARANCE
    WRIST_DEPTH = max(SIDE_HOVER, SWING_CLEARANCE)  # camera at the turn pose's tip, this far above the top
    fix, at = run(0.01)
    assert fix == pytest.approx(0.01, abs=0.001) and at[0] == pytest.approx(0.27, abs=0.001)
    fix, at = run(0.05)  # 5 cm "fix" = a wrong camera model: ignored, overhead position used
    assert fix == 0.0 and at[0] == pytest.approx(0.26, abs=0.001)
