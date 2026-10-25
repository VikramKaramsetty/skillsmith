"""WS3.5: WorldModel live tracking on synthetic detections (no camera, no cv2)."""

import time

import pytest
from ws3c_fakes import Det, FakeArm, FakeDetector, FakeFrames, PassCamera, _fixture_config, cup, make_world

from skillsmith.config import Config
from skillsmith.events import NullBus
from skillsmith.hal.base import Env
from skillsmith.hal.fake import FakeWorld
from skillsmith.perception.world import WorldModel
from skillsmith.primitives import Robot
from skillsmith.safety import SafetyGuard
from skillsmith.types import WorldChanged


def obj(world, id_):
    return next(o for o in world.observe().objects if o.id == id_)


def test_stable_ids_and_positions():
    det = FakeDetector([cup("red", 0.22, 0.08), cup("blue", 0.22, -0.08), cup("red", 0.15, 0.0)])
    world = make_world(detector=det)
    world.step()
    ids = {o.id for o in world.observe().objects}
    assert ids == {"red_cup_0", "blue_cup_0", "red_cup_1"}
    det.dets = [cup("red", 0.151, 0.002), cup("blue", 0.221, -0.08), cup("red", 0.222, 0.081)]  # reordered, jitter
    world.step()
    assert obj(world, "red_cup_0").x == pytest.approx(0.222)
    assert obj(world, "red_cup_1").x == pytest.approx(0.151)
    scene = world.observe()
    assert scene.source == "camera:front" and scene.t == 1002.0
    red = scene.find("red cup")
    assert red is not None and red.height == pytest.approx(0.08) and red.z == 0.0
    assert world.unexpected_motion() is None  # jitter under 1 cm is not a bump


def test_ray_cast_at_known_top_height_and_stacked():
    camera = PassCamera()
    det = FakeDetector([cup("red", 0.22, 0.08)])
    world = make_world(detector=det, camera=camera)
    world.step()
    assert camera.planes == [pytest.approx(0.08)]
    det.dets = [cup("red", 0.22, 0.08), cup("blue", 0.221, 0.08)]  # blue sits on red
    camera.planes.clear()
    world.step()
    blue = world.observe().find("blue cup")
    assert blue.z == pytest.approx(0.08) and blue.top == pytest.approx(0.16)
    assert camera.planes[-1] == pytest.approx(0.16)


def test_hidden_object_keeps_last_position():
    det = FakeDetector([cup("red", 0.22, 0.08), cup("blue", 0.22, -0.08)])
    world = make_world(detector=det)
    world.step()
    det.dets = [cup("blue", 0.22, -0.08)]  # red is behind the arm
    for _ in range(5):
        world.step()
    red = obj(world, "red_cup_0")
    assert not red.visible and (red.x, red.y) == (0.22, 0.08)
    assert world.unexpected_motion() is None
    det.dets = [cup("red", 0.223, 0.08), cup("blue", 0.22, -0.08)]  # back where it was
    world.step()
    assert obj(world, "red_cup_0").visible
    assert {o.id for o in world.observe().objects} == {"red_cup_0", "blue_cup_0"}
    assert world.unexpected_motion() is None


def test_bump_fires_once_with_readable_message():
    det = FakeDetector([cup("red", 0.22, 0.08)])
    world = make_world(detector=det)
    world.step()
    det.dets = [cup("red", 0.22, 0.112)]  # pushed 3.2 cm toward +y
    world.step()
    assert world.unexpected_motion() is None  # one frame could be noise
    world.step()
    assert world.unexpected_motion() == "red cup moved 3.2 cm toward +y while not held"
    assert world.unexpected_motion() is None  # cleared once read
    for _ in range(5):
        world.step()  # it stays at the new place: no repeat
    assert world.unexpected_motion() is None


def test_single_noisy_frame_is_not_a_bump():
    det = FakeDetector([cup("red", 0.22, 0.08)])
    world = make_world(detector=det)
    world.step()
    det.dets = [cup("red", 0.24, 0.08)]
    world.step()
    det.dets = [cup("red", 0.22, 0.08)]
    world.step()
    world.step()
    assert world.unexpected_motion() is None


def test_held_object_follows_gripper_and_is_not_a_bump():
    arm = FakeArm(tip=(0.22, 0.08, 0.04))
    det = FakeDetector([cup("red", 0.22, 0.08), cup("blue", 0.22, -0.08)])
    world = make_world(detector=det, arm=arm)
    world.step()
    arm.holding = True
    world.step()
    det.dets = [cup("blue", 0.22, -0.08)]  # the arm covers the red cup while carrying it
    for x, y, z in [(0.22, 0.08, 0.15), (0.2, 0.0, 0.18), (0.22, -0.08, 0.20)]:
        arm.move_tip(x, y, z)
        world.step()
        red = obj(world, "red_cup_0")
        assert (red.x, red.y) == pytest.approx((x, y)) and red.z == pytest.approx(z - 0.04)
        assert red.visible
    det.dets = [cup("blue", 0.22, -0.08), cup("red", 0.22, -0.075)]  # seen right next to the tip
    world.step()
    assert len(world.observe().objects) == 2  # absorbed by the held track, no duplicate
    assert world.unexpected_motion() is None


def test_release_reanchors_without_bump_then_bump_detectable():
    arm = FakeArm(tip=(0.22, 0.08, 0.04))
    det = FakeDetector([cup("red", 0.22, 0.08), cup("blue", 0.22, -0.08)])
    world = make_world(detector=det, arm=arm)
    world.step()
    arm.holding = True
    world.step()
    arm.move_tip(0.18, 0.0, 0.05)
    world.step()
    arm.holding = False
    world.step()
    det.dets = [cup("red", 0.185, 0.006), cup("blue", 0.22, -0.08)]  # landed ~0.8 cm off the FK guess
    for _ in range(3):
        world.step()
    assert world.unexpected_motion() is None
    assert obj(world, "red_cup_0").x == pytest.approx(0.185)
    det.dets = [cup("red", 0.185, 0.006), cup("blue", 0.19, -0.08)]  # blue is knocked toward -x
    world.step()
    world.step()
    assert world.unexpected_motion() == "blue cup moved 3.0 cm toward -x while not held"


def test_low_confidence_detections_do_not_create_tracks():
    world = make_world([cup("red", 0.22, 0.08, confidence=0.2)])
    world.step()
    assert world.observe().objects == ()


def test_frame_errors_emit_one_event_and_keep_last_scene():
    bus = NullBus()
    frames = FakeFrames()
    world = make_world([cup("red", 0.22, 0.08)], frames=frames, bus=bus)
    assert world.step()
    frames.fail = "robotd refused /frame/front: camera gone"
    assert not world.step() and not world.step()
    errors = [e for e in bus.events if e["type"] == "error"]
    assert len(errors) == 1 and "camera gone" in errors[0]["data"]["message"]
    assert errors[0]["data"]["where"] == "perception.world"
    assert world.observe().find("red cup") is not None
    frames.fail = None
    assert world.step()
    assert bus.types()[-1] == "note"


def test_snapshot_writes_latest_frame(tmp_path):
    world = make_world([cup("red", 0.22, 0.08)])
    assert world.snapshot(str(tmp_path / "x.jpg")) is None
    world.step()
    path = world.snapshot(str(tmp_path / "k" / "x.jpg"))
    assert path and open(path, "rb").read().startswith(b"\xff\xd8")


def test_object_sizes_from_config():
    cfg = _fixture_config()
    data = dict(cfg.raw())
    data["world"] = {**data["world"], "objects": {"cup": {"height": 0.1, "width": 0.07}}}
    world = make_world([cup("red", 0.22, 0.08)], cfg=Config(data))
    red = world.observe().find("red cup")
    assert (red.height, red.width) == (0.1, 0.07)


def test_background_loop_runs_at_configured_rate():
    frames = FakeFrames()
    world = make_world([cup("red", 0.22, 0.08)], frames=frames).start()
    try:
        time.sleep(0.6)
        assert 9.0 <= world.rate() <= 15.0  # rate_hz: 12 in robot.yaml
        assert world.observe().find("red cup") is not None
    finally:
        world.close()
    assert frames.closed


def test_drops_into_fake_env_and_robot_raises_world_changed():
    """WorldModel as the Perception of a fake Env; detections come from the fake world's truth."""
    cfg = _fixture_config()
    truth = FakeWorld.demo(cfg)

    def detections():
        return [Det(o.label, o.color, o.kind, (o.x, o.y)) for o in truth.observe().objects]

    world = WorldModel(cfg, FakeFrames(), kinematics=truth, arm=truth,
                       detector=FakeDetector(fn=detections), camera=PassCamera(), decode=lambda f: f)
    env = Env("fake", truth, truth, world)
    robot = Robot(env, SafetyGuard(cfg), NullBus(), cfg)
    world.step()
    blue = world.observe().require("blue cup")
    for z in (0.12, 0.04):
        robot.move_to(blue.x, blue.y, z)
        world.step()
    assert robot.grasp()
    world.step()
    for x, y in ((blue.x, blue.y), (0.2, 0.0)):
        robot.move_to(x, y, 0.15)
        world.step()
    held = world.observe().require("blue cup")
    assert (held.x, held.y) == pytest.approx((0.2, 0.0))
    assert world.unexpected_motion() is None
    red = next(o for o in truth.objects if o.label == "red cup")
    red.y += 0.03  # someone bumps the red cup
    world.step()
    world.step()
    with pytest.raises(WorldChanged, match="red cup moved 3.0 cm toward \\+y while not held"):
        robot.move_to(0.2, 0.0, 0.12)
    env.close()


def test_default_detector_prompts_yolo_with_the_configured_labels(monkeypatch):
    from skillsmith.perception import color, world

    built = []
    monkeypatch.setattr(color, "make_detector", lambda cfg, labels=None, bus=None: built.append(labels) or "det")
    assert world._default_detector({"detect": {"labels": ["white cup", "red cup"]}}) == "det"
    assert world._default_detector({}) == "det"
    assert built == [["white cup", "red cup"], None]  # no labels: make_detector falls back to HSV


def test_the_arm_hovering_over_a_cup_is_not_a_bump():
    arm = FakeArm(tip=(0.10, 0.15, 0.20))  # far from the cup
    det = FakeDetector([cup("white", 0.15, -0.10)])
    world = make_world(detector=det, arm=arm)
    world.step()
    arm.move_tip(0.15, -0.10, 0.18)  # over the cup: its detection shifts 1.5 cm (arm merged with it)
    det.dets = [cup("white", 0.165, -0.093)]
    for _ in range(10):
        world.step()
    white = obj(world, "white_cup_0")
    assert (white.x, white.y) == pytest.approx((0.15, -0.10))
    assert world.unexpected_motion() is None
    arm.move_tip(0.10, 0.15, 0.20)  # the arm leaves: a real move is a bump again
    for _ in range(10):
        world.step()
    assert world.unexpected_motion() is not None


def test_a_split_top_is_one_cup_not_two():
    det = FakeDetector([cup("pink", 0.20, 0.06)])
    world = make_world(detector=det)
    world.step()
    det.dets = [cup("pink", 0.19, 0.05), cup("pink", 0.215, 0.075)]  # the gripper splits the pink top
    for _ in range(5):
        world.step()
    assert [o.id for o in world.observe().objects] == ["pink_cup_0"]


def test_a_cup_moved_by_hand_before_an_attempt_is_not_a_bump_of_it():
    det = FakeDetector([cup("red", 0.22, 0.08)])
    world = make_world(detector=det)
    world.step()
    det.dets = [cup("red", 0.27, 0.05)]  # moved while the run waited at the approval prompt
    for _ in range(5):
        world.step()
    world.reset_gate()  # the attempt starts
    assert world.unexpected_motion() is None
    assert obj(world, "red_cup_0").x == pytest.approx(0.27)
    det.dets = [cup("red", 0.30, 0.05)]  # but a move during the attempt still is one
    for _ in range(5):
        world.step()
    assert "moved" in (world.unexpected_motion() or "")


def test_a_lifted_held_cup_is_neither_a_new_cup_nor_a_bump_and_survives_a_holding_flicker():
    det = FakeDetector([cup("red", 0.29, 0.0)])
    arm = FakeArm(tip=(0.31, 0.0, 0.07))
    world = make_world(detector=det, arm=arm)
    world.step()
    arm.holding = True
    world.step()  # grabbed: 2 cm from the tip
    det.dets = [cup("red", 0.33, 0.0)]  # lifted: its top is higher than the localiser assumes, so it lands off
    arm.move_tip(0.31, 0.0, 0.12)
    for k in range(4):
        arm.holding = k != 1  # one flicker mid-lift
        world.step()
    assert world.unexpected_motion() is None
    assert len(world.observe().objects) == 1


def test_displacement_sees_a_cup_pushed_while_the_arm_occludes_it():
    det = FakeDetector([cup("red", 0.29, 0.0)])
    arm = FakeArm(tip=(0.29, 0.0, 0.07))  # tip right at it: the occlusion rule keeps the track in place
    world = make_world(detector=det, arm=arm)
    world.step()
    det.dets = [cup("red", 0.29, -0.06)]
    for _ in range(3):
        world.step()
    assert world.displacement("red_cup_0", (0.29, 0.0)) == pytest.approx(0.06, abs=0.002)
