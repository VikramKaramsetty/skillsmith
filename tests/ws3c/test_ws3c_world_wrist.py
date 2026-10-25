"""WS3.7 wiring: WorldModel.align_offset / holding_check delegate to perception.wrist and never raise."""

from __future__ import annotations

import pytest
from ws3c_fakes import Det, FakeArm, FakeFrames, _fixture_config, make_world

from skillsmith.events import NullBus
from skillsmith.hal.base import Env
from skillsmith.hal.fake import FakeWorld
from skillsmith.perception.frames import FrameError, RobotdFrames
from skillsmith.perception.world import _default_wrist_frames
from skillsmith.skills.runtime import run_skill
from skillsmith.types import SceneObject

cfg = _fixture_config()  # without the rig's pink-only colour table
# These tests exercise the wrist logic, not the product setting: robot.yaml keeps fine alignment off
# (min_align_confidence 1.1) until the wrist camera is measured, so use the code's default threshold.
cfg._data = {**cfg._data, "wrist": {**(cfg._data.get("wrist") or {}), "min_align_confidence": 0.3}}
_fake_world = make_world


def make_world(*args, **kw):
    kw.setdefault("cfg", cfg)
    return _fake_world(*args, **kw)
BLOCK = SceneObject("blue_block_0", "blue block", "blue", "block", 0.2, 0.0, 0.0, 0.03, 0.03)


class FailingWrist:
    def __init__(self, message: str = "cannot reach robotd at http://x/frame/wrist: refused"):
        self.message, self.reads, self.closed = message, 0, False

    def read(self):
        self.reads += 1
        raise FrameError(self.message)

    def close(self) -> None:
        self.closed = True


class ImageWrist:
    """Returns a decoded image directly (used with wrist_decode=lambda f: f)."""

    def __init__(self, image):
        self.image = image

    def read(self):
        return self.image

    def close(self) -> None:
        pass


def wrist_errors(bus: NullBus) -> list:
    return [e for e in bus.events if e["type"] == "error" and e["data"]["where"] == "perception.wrist"]


# ---------------------------------------------------------------- failures degrade, never raise


def test_failing_wrist_source_gives_zero_offset_and_one_error_event():
    bus, wrist = NullBus(), FailingWrist()
    world = make_world(bus=bus, wrist_frames=wrist)
    assert world.align_offset(BLOCK) == (0.0, 0.0)
    errors = wrist_errors(bus)
    assert len(errors) == 1 and wrist.reads == 1
    assert "coarse alignment only" in errors[0]["data"]["message"] and "refused" in errors[0]["data"]["message"]


def test_no_wrist_camera_configured_gives_zero_offset():
    bus = NullBus()
    world = make_world(bus=bus)  # fake front frames -> no default wrist source
    assert world.wrist_frames is None
    assert world.align_offset(BLOCK) == (0.0, 0.0)
    assert "no wrist camera configured" in wrist_errors(bus)[0]["data"]["message"]


def test_decode_failure_like_missing_cv2_gives_zero_offset():
    def no_cv2(_frame):
        raise ImportError("No module named 'cv2'")

    bus = NullBus()
    world = make_world(bus=bus, wrist_frames=FakeFrames(), wrist_decode=no_cv2)
    assert world.align_offset(BLOCK) == (0.0, 0.0)
    assert "ImportError" in wrist_errors(bus)[0]["data"]["message"]


def test_a_failing_event_bus_does_not_turn_degradation_into_a_crash():
    class BrokenBus(NullBus):
        def emit(self, type_, **data):
            raise OSError("disk full")

    world = make_world(bus=BrokenBus(), wrist_frames=FailingWrist())
    assert world.align_offset(BLOCK) == (0.0, 0.0)


def test_holding_check_falls_back_to_the_gripper_signal():
    bus, arm = NullBus(), FakeArm()
    arm.holding = True
    world = make_world(arm=arm, bus=bus, wrist_frames=FailingWrist())
    held, reason = world.holding_check()
    assert held is True and "gripper signal: holding" in reason and "could not judge" in reason
    assert len(wrist_errors(bus)) == 1
    arm.holding = False
    assert world.holding_check()[0] is False


def test_holding_check_without_arm_or_camera_answers_not_confirmed():
    world = make_world()
    held, reason = world.holding_check()
    assert held is False and "no wrist camera configured" in reason and "no gripper signal" in reason


def test_default_wrist_source_follows_the_front_source():
    front = RobotdFrames("http://robotd:8000", "front")
    wrist = _default_wrist_frames(cfg, front)
    assert isinstance(wrist, RobotdFrames) and wrist.url.endswith("/frame/wrist")
    assert isinstance(_default_wrist_frames(cfg, None), RobotdFrames)
    assert _default_wrist_frames(cfg, FakeFrames()) is None


def test_close_closes_the_wrist_source():
    wrist = FailingWrist()
    world = make_world(wrist_frames=wrist)
    world.close()
    assert wrist.closed


def test_align_over_completes_on_a_world_model_with_a_failing_wrist():
    fake = FakeWorld.demo(cfg)
    bus = NullBus()
    block = Det("green block", "green", "block", (0.15, 0.0))
    world = make_world([block], bus=bus, wrist_frames=FailingWrist())
    world.arm, world.kin = fake, fake
    assert world.step()
    env = Env("fake", fake, fake, world)
    src = ('SKILL = {"name": "t", "params": {}, "uses": []}\n\n'
           "def run(robot, scene, skills):\n    robot.align_over('green block', hover=0.05)\n")
    result = run_skill(src, env, {}, cfg, bus, skill="t")
    assert result.ok, result.reason()
    x, y, z = fake.fk(fake.joints())
    assert (round(x, 3), round(y, 3), round(z, 3)) == (0.15, 0.0, 0.08)  # coarse: block top 0.03 + 0.05
    assert len(wrist_errors(bus)) == 1


# ---------------------------------------------------------------- real wrist logic (needs cv2)


def _frame(cx: int, cy: int, half: int, colour=(200, 60, 20)):
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    img = np.full((480, 640, 3), (130, 128, 125), np.uint8)
    img[max(cy - half, 0):cy + half, max(cx - half, 0):cx + half] = colour
    return img


def test_synthetic_wrist_frame_gives_a_nonzero_offset_and_a_note():
    from skillsmith.perception import wrist

    image = _frame(380, 240, 30)  # blue block 60 px right of centre
    bus = NullBus()
    world = make_world(bus=bus, wrist_frames=ImageWrist(image), wrist_decode=lambda f: f, tool_yaw=lambda: 0.0)
    dx, dy = world.align_offset(BLOCK)
    assert (dx, dy) != (0.0, 0.0)
    assert (dx, dy) == wrist.align_offset(image, "blue", cfg, yaw_deg=0.0)
    assert not wrist_errors(bus)
    note = [e for e in bus.events if e["type"] == "note"][-1]["data"]
    assert note["where"] == "perception.wrist" and (note["dx"], note["dy"]) == (dx, dy)
    assert note["confidence"] >= 0.3 and "wrist align" in note["text"]


def test_yaw_from_injected_callable_rotates_the_offset():
    image = _frame(380, 240, 30)
    straight = make_world(wrist_frames=ImageWrist(image), wrist_decode=lambda f: f, tool_yaw=lambda: 0.0)
    turned = make_world(wrist_frames=ImageWrist(image), wrist_decode=lambda f: f, tool_yaw=lambda: 90.0)
    dx0, dy0 = straight.align_offset(BLOCK)
    dx1, dy1 = turned.align_offset(BLOCK)
    assert dx1 == pytest.approx(-dy0, abs=1e-4) and dy1 == pytest.approx(dx0, abs=1e-4)


def test_low_confidence_blob_is_ignored():
    image = _frame(0, 0, 60)  # in the corner: touches the edge and is clamped -> confidence 0.25
    bus = NullBus()
    world = make_world(bus=bus, wrist_frames=ImageWrist(image), wrist_decode=lambda f: f)
    assert world.align_offset(BLOCK) == (0.0, 0.0)
    assert "low confidence" in wrist_errors(bus)[0]["data"]["message"]


def test_no_blob_in_view_is_zero_offset():
    image = _frame(320, 240, 30, colour=(30, 30, 210))  # red, not blue
    bus = NullBus()
    world = make_world(bus=bus, wrist_frames=ImageWrist(image), wrist_decode=lambda f: f)
    assert world.align_offset(BLOCK) == (0.0, 0.0)
    assert "no blue" in wrist_errors(bus)[0]["data"]["message"]


def test_holding_check_uses_the_wrist_frame_when_it_can_judge():
    image = _frame(320, 330, 80)
    arm = FakeArm()  # gripper says not holding; the camera sees the block between the fingers
    world = make_world(arm=arm, wrist_frames=ImageWrist(image), wrist_decode=lambda f: f)
    held, reason = world.holding_check()
    assert held is True and reason.startswith("wrist camera:")


def test_dark_wrist_frame_falls_back_to_the_gripper():
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    arm = FakeArm()
    arm.holding = True
    bus = NullBus()
    dark = np.zeros((480, 640, 3), np.uint8)
    world = make_world(arm=arm, bus=bus, wrist_frames=ImageWrist(dark), wrist_decode=lambda f: f)
    held, reason = world.holding_check()
    assert held is True and "too dark" in reason and "gripper signal" in reason
    assert len(wrist_errors(bus)) == 1
