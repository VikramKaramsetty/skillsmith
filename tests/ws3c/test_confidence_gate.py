"""WS3.6: the perception confidence gate flips on marker drift, low confidence and object-count
changes (synthetic detections, no camera, no cv2)."""

from ws3c_fakes import FakeArm, FakeDetector, FakeFrames, PassCamera, cup, make_world

from skillsmith.config import Config, load_config
from skillsmith.events import NullBus


def gate_cfg(**world):
    data = dict(load_config().raw())
    data["world"] = {**data["world"], "drift_every": 1, "lost_s": 0.5, **world}  # lost after 6 frames
    return Config(data)


def two_cups(**kw):
    det = FakeDetector([cup("red", 0.22, 0.08), cup("blue", 0.22, -0.08)])
    return det, make_world(detector=det, cfg=gate_cfg(), **kw)


def steps(world, n):
    for _ in range(n):
        assert world.step()


def test_ok_on_a_steady_table():
    camera = PassCamera(drift=0.002)
    _det, world = two_cups(camera=camera)
    steps(world, 5)
    ok, reason = world.perception_ok()
    assert ok, reason
    assert reason == "perception ok: markers 0.2 cm, 2 objects stable, confidence fine"
    assert camera.drift_checks == 5


def test_no_update_yet_is_not_ok():
    world = make_world([cup("red", 0.22, 0.08)], cfg=gate_cfg())
    ok, reason = world.perception_ok()
    assert not ok and reason.startswith("world model stale")


def test_marker_drift_flips_the_gate_after_two_checks():
    camera = PassCamera()
    _det, world = two_cups(camera=camera)
    steps(world, 2)
    camera.drift = 0.021  # someone bumped the camera
    world.step()
    assert world.perception_ok()[0]  # one check over the limit could be noise
    world.step()
    ok, reason = world.perception_ok()
    assert not ok
    assert reason == "marker drift 2.1 cm (limit 1.0 cm): camera moved, recalibrate"
    world.reset_gate()
    world.step()
    assert not world.perception_ok()[0]  # drift is the camera's state; a new attempt keeps it
    camera.drift = 0.001  # re-seated
    world.step()
    assert world.perception_ok()[0]


def test_drift_check_runs_every_n_updates():
    camera = PassCamera()
    world = make_world([cup("red", 0.22, 0.08)], camera=camera, cfg=gate_cfg(drift_every=4))
    steps(world, 9)
    assert camera.drift_checks == 3  # updates 0, 4, 8


def test_missing_marker_check_is_reported_once_not_silent():
    class NoMarkers(PassCamera):
        def marker_drift(self, _frame):
            raise NotImplementedError("WS3.6")

    bus = NullBus()
    world = make_world([cup("red", 0.22, 0.08)], camera=NoMarkers(), bus=bus, cfg=gate_cfg())
    steps(world, 3)
    errors = [e["data"] for e in bus.events if e["type"] == "error"]
    assert len(errors) == 1 and "marker drift not checked" in errors[0]["message"]
    ok, reason = world.perception_ok()
    assert ok and "markers unchecked" in reason


def test_falls_back_to_marker_errors_while_marker_drift_is_a_stub():
    class ErrorsOnly(PassCamera):
        def __init__(self):
            super().__init__()
            self.errors = {0: 0.002, 1: 0.004, 2: 0.03}  # one misdetection; the median ignores it

        def marker_drift(self, _frame):
            raise NotImplementedError("WS3.6")

        def marker_errors(self, _frame):
            return dict(self.errors)

    bus = NullBus()
    camera = ErrorsOnly()
    world = make_world([cup("red", 0.22, 0.08)], camera=camera, bus=bus, cfg=gate_cfg())
    steps(world, 3)
    ok, reason = world.perception_ok()
    assert ok and "markers 0.4 cm" in reason
    assert not [e for e in bus.events if e["type"] == "error"]
    camera.errors = {}  # markers hidden: keep the last measurement
    steps(world, 3)
    assert "markers 0.4 cm" in world.perception_ok()[1]
    camera.errors = {0: 0.02, 1: 0.025}  # camera moved
    steps(world, 2)
    ok, reason = world.perception_ok()
    assert not ok and reason.startswith("marker drift 2.2 cm")


def test_occluded_markers_keep_the_last_measurement():
    camera = PassCamera(drift=0.003)
    world = make_world([cup("red", 0.22, 0.08)], camera=camera, cfg=gate_cfg())
    world.step()
    camera.drift = float("nan")  # markers hidden behind the arm
    steps(world, 3)
    ok, reason = world.perception_ok()
    assert ok and "markers 0.3 cm" in reason


def test_persistent_low_confidence_flips_and_latches_until_reset():
    det, world = two_cups()
    steps(world, 2)
    det.dets = [cup("red", 0.22, 0.08), cup("blue", 0.22, -0.08), cup("green", 0.15, 0.12, confidence=0.31)]
    steps(world, 2)
    assert world.perception_ok()[0]  # two frames: not yet
    world.step()
    ok, reason = world.perception_ok()
    assert not ok and reason == "low confidence: green cup at 0.31 (< 0.50) for 3 frames"
    det.dets = det.dets[:2]
    world.step()
    assert not world.perception_ok()[0]  # latched for the attempt
    world.reset_gate()
    world.step()
    assert world.perception_ok()[0]


def test_flickering_low_confidence_is_noise():
    det, world = two_cups()
    sure = [cup("red", 0.22, 0.08), cup("blue", 0.22, -0.08)]
    doubt = sure + [cup("green", 0.15, 0.12, confidence=0.3)]
    for dets in (doubt, doubt, sure, doubt, sure, doubt, doubt, sure):
        det.dets = dets
        world.step()
    assert world.perception_ok()[0]


def test_low_confidence_next_to_the_arm_is_occlusion_not_doubt():
    arm = FakeArm(tip=(0.22, 0.08, 0.04))
    det = FakeDetector([cup("red", 0.22, 0.08), cup("blue", 0.22, -0.08)])
    world = make_world(detector=det, arm=arm, cfg=gate_cfg())
    world.step()
    arm.holding = True
    world.step()
    for x, y in [(0.2, 0.04), (0.2, 0.0), (0.21, -0.03)]:
        arm.move_tip(x, y, 0.15)
        det.dets = [cup("blue", 0.22, -0.08), cup("red", x, y + 0.01, confidence=0.2)]  # fingers cover the cup
        world.step()
    ok, reason = world.perception_ok()
    assert ok, reason


def test_new_object_changes_the_count():
    det, world = two_cups()
    steps(world, 2)
    det.dets = det.dets + [cup("green", 0.15, 0.12)]
    steps(world, 2)
    assert world.perception_ok()[0]  # a new track must persist
    world.step()
    ok, reason = world.perception_ok()
    assert not ok and reason == "object count changed 2 → 3: green cup appeared"
    world.reset_gate()
    world.step()
    ok, reason = world.perception_ok()
    assert ok and "3 objects stable" in reason


def test_one_frame_false_positive_is_not_a_count_change():
    det, world = two_cups()
    world.step()
    det.dets = det.dets + [cup("green", 0.15, 0.12)]
    world.step()
    det.dets = det.dets[:2]
    steps(world, 4)
    assert world.perception_ok()[0]


def test_object_unseen_too_long_changes_the_count_but_hidden_briefly_does_not():
    det, world = two_cups()
    world.step()
    det.dets = [cup("blue", 0.22, -0.08)]  # red goes behind the arm...
    steps(world, 5)
    assert world.perception_ok()[0]
    world.step()  # ...and stays gone for 6 frames (lost_s 0.5 at 12 Hz)
    ok, reason = world.perception_ok()
    assert not ok and reason == "object count changed 2 → 1: red cup unseen for 0.5 s"
    det.dets = [cup("red", 0.22, 0.08), cup("blue", 0.22, -0.08)]
    world.step()
    assert world.perception_ok()[0]


def test_held_object_never_counts_as_missing():
    arm = FakeArm(tip=(0.22, 0.08, 0.04))
    det = FakeDetector([cup("red", 0.22, 0.08), cup("blue", 0.22, -0.08)])
    world = make_world(detector=det, arm=arm, cfg=gate_cfg())
    world.step()
    arm.holding = True
    world.step()
    det.dets = [cup("blue", 0.22, -0.08)]
    arm.move_tip(0.2, 0.0, 0.15)
    steps(world, 10)
    ok, reason = world.perception_ok()
    assert ok, reason


def test_stale_world_model_fails_the_gate_and_issues_combine():
    frames = FakeFrames()
    camera = PassCamera()
    _det, world = two_cups(frames=frames, camera=camera)
    camera.drift = 0.03
    steps(world, 2)
    frames.fail = "camera gone"
    assert not world.step()
    ok, reason = world.perception_ok()
    assert not ok
    issues = reason.split("; ")
    assert issues[0] == "world model stale: FrameError: camera gone"
    assert issues[1].startswith("marker drift 3.0 cm")
