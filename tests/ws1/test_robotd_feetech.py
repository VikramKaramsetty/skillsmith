"""WS1.2: robotd's Feetech backend over a fake LeRobot SO101 follower (no LeRobot, no serial port)."""

import sys
from types import SimpleNamespace

import pytest

from robotd.controller import JOINT_NAMES, ArmController
from robotd.feetech import FeetechBus, connect_follower

STALE = {name: 0.0 for name in JOINT_NAMES}  # the goal left in the servos by an earlier session
HAND = {"shoulder_pan": 12.0, "shoulder_lift": -40.0, "elbow_flex": 35.0, "wrist_flex": 60.0,
        "wrist_roll": -5.0, "gripper": 20.0}  # where the arm was put by hand while limp


class FakeMotorsBus:
    """Stands in for LeRobot's motors bus: servos drive to Goal_Position whenever torque is on."""

    def __init__(self, present=None, goal=None, torque=False, load_raw=0):
        self.present = dict(present or HAND)
        self.goal = dict(goal or STALE)
        self.torque = {name: torque for name in JOINT_NAMES}
        self.load_raw = load_raw
        self.calls = []

    def connect(self):
        self.calls.append(("connect",))

    def disconnect(self, disable_torque=True):
        self.calls.append(("disconnect", disable_torque))

    def sync_read(self, data_name, motors=None, *, normalize=True, num_retry=0):
        if data_name == "Present_Position":
            return dict(reversed(list(self.present.items())))  # LeRobot's order is not ours to rely on
        assert data_name == "Torque_Enable"
        return {name: int(on) for name, on in self.torque.items()}

    def sync_write(self, data_name, values, *, normalize=True, num_retry=0):
        assert data_name == "Goal_Position"
        self.goal.update(values)
        self.calls.append(("goal", dict(values)))
        self._drive()

    def read(self, data_name, motor, *, normalize=True, num_retry=0):
        assert (data_name, motor) == ("Present_Load", "gripper")
        return self.load_raw

    def enable_torque(self, motors=None, num_retry=0):
        self.torque = {name: True for name in JOINT_NAMES}
        self.calls.append(("torque", True))
        self._drive()

    def disable_torque(self, motors=None, num_retry=0):
        self.torque = {name: False for name in JOINT_NAMES}
        self.calls.append(("torque", False))

    def _drive(self):
        for name, on in self.torque.items():
            if on:
                self.present[name] = self.goal[name]


def cal(range_min, range_max):
    """The fields of LeRobot's MotorCalibration that matter here (raw STS3215 ticks, 0..4095)."""
    return SimpleNamespace(range_min=range_min, range_max=range_max)


CALIBRATION = {"shoulder_pan": cal(1000, 3000), "shoulder_lift": cal(800, 3200), "elbow_flex": cal(900, 3100),
               "wrist_flex": cal(850, 3150), "wrist_roll": cal(0, 4095), "gripper": cal(1390, 2900)}


class FakeFollower:
    """Stands in for LeRobot's SO101Follower: `configure` frees the motors, sets them up, re-energises."""

    def __init__(self, bus=None, calibrated=True):
        self.bus = bus or FakeMotorsBus()
        self.is_calibrated = calibrated
        self.calibration = CALIBRATION

    def configure(self):
        self.bus.disable_torque()
        self.bus.calls.append(("configure",))
        self.bus.enable_torque()

    def disconnect(self):
        self.bus.disconnect(True)


def connect(follower):
    return connect_follower("/dev/ttyFAKE", "follower", make_robot=lambda port, calibration_id: follower)


def test_connect_holds_the_pose_the_arm_is_in_instead_of_a_stale_goal():
    follower = FakeFollower()
    connect(follower)
    assert follower.bus.present == HAND
    calls = follower.bus.calls
    assert calls.index(("goal", HAND)) < calls.index(("torque", True))


def test_connect_passes_the_port_and_calibration_id_to_lerobot():
    seen = []

    def make_robot(port, calibration_id):
        seen.append((port, calibration_id))
        return FakeFollower()

    connect_follower("/dev/serial/by-id/arm", "follower", make_robot=make_robot)
    assert seen == [("/dev/serial/by-id/arm", "follower")]


def test_connect_refuses_a_calibration_mismatch_without_touching_the_motors():
    follower = FakeFollower(calibrated=False)
    with pytest.raises(RuntimeError, match="calibrat"):
        connect(follower)
    assert follower.bus.calls == [("connect",), ("disconnect", False)]


def test_without_lerobot_the_error_names_the_python_to_use(monkeypatch):
    monkeypatch.setitem(sys.modules, "lerobot", None)
    with pytest.raises(ImportError, match=r"~/robot/\.venv"):
        connect_follower("/dev/ttyFAKE", "follower")


def test_joint_limits_are_the_calibrated_ranges_in_degrees():
    # degrees = (ticks - mid) * 360 / 4095, so a range of 2000 ticks is +-1000 * 360 / 4095 = +-87.91 deg
    limits = FeetechBus(FakeFollower()).joint_limits()
    assert limits == [pytest.approx((-87.912, 87.912), abs=1e-3), pytest.approx((-105.495, 105.495), abs=1e-3),
                      pytest.approx((-96.703, 96.703), abs=1e-3), pytest.approx((-101.099, 101.099), abs=1e-3),
                      pytest.approx((-180.0, 180.0), abs=1e-3), (0.0, 100.0)]


def test_present_positions_come_back_in_joint_order():
    bus = FeetechBus(FakeFollower())
    assert bus.read_present() == [12.0, -40.0, 35.0, 60.0, -5.0, 20.0]


def test_goal_is_written_per_joint_name():
    follower = FakeFollower()
    FeetechBus(follower).write_goal([1, 2, 3, 4, 5, 6])
    assert follower.bus.calls[-1] == ("goal", {"shoulder_pan": 1.0, "shoulder_lift": 2.0, "elbow_flex": 3.0,
                                               "wrist_flex": 4.0, "wrist_roll": 5.0, "gripper": 6.0})


@pytest.mark.parametrize("raw", [300, -300, 1024 + 300])  # plain, LeRobot-decoded sign, raw sign bit 10
def test_gripper_load_is_a_magnitude(raw):
    assert FeetechBus(FakeFollower(FakeMotorsBus(load_raw=raw))).read_gripper_load() == 300.0


def test_torque_counts_as_on_only_when_every_motor_has_it():
    follower = FakeFollower(FakeMotorsBus(torque=True))
    bus = FeetechBus(follower)
    assert bus.torque_enabled() is True
    follower.bus.torque["wrist_roll"] = False
    assert bus.torque_enabled() is False


def test_set_torque_switches_every_motor():
    follower = FakeFollower()
    bus = FeetechBus(follower)
    bus.set_torque(True)
    bus.set_torque(False)
    assert follower.bus.calls[-2:] == [("torque", True), ("torque", False)]


def test_close_disconnects_the_follower():
    follower = FakeFollower()
    FeetechBus(follower).close()
    assert follower.bus.calls[-1] == ("disconnect", True)


def test_the_controller_drives_the_arm_through_lerobot():
    follower = FakeFollower()
    ctl = ArmController(connect(follower), max_step_deg=10, max_speed_dps=1000, settle_timeout_s=0.3)
    ctl.move_joints([32, -40, 35, 60, -5, 100], speed_dps=1000)
    assert follower.bus.present["shoulder_pan"] == pytest.approx(32)
    assert follower.bus.present["gripper"] == 20.0  # moves never touch the jaws
