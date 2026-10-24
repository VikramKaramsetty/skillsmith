"""WS1.1: robotd's arm controller on the mock bus (no FastAPI needed)."""

import math
import threading
import time

import pytest
from conftest import StuckElbowBus, goals

from robotd.controller import ArmController, RobotdError
from robotd.mock import MockBus

OPEN = 100.0


def test_move_reaches_the_target(ctl, bus):
    ctl.move_joints([20, -10, 30, 5, -15, OPEN], speed_dps=1000)
    assert bus.present[:5] == pytest.approx([20, -10, 30, 5, -15])


def test_large_move_is_split_into_steps_no_bigger_than_max_step(ctl, bus):
    ctl.move_joints([45, 0, 0, 0, 0, OPEN], speed_dps=1000)
    assert goals(bus) == pytest.approx([9, 18, 27, 36, 45])


def test_move_starts_from_where_the_arm_actually_is(ctl, bus):
    bus.present[0] = 4.0  # sagged or nudged since the last goal
    ctl.move_joints([24, 0, 0, 0, 0, OPEN], speed_dps=1000)
    assert goals(bus) == pytest.approx([14, 24])


def test_move_is_paced_by_speed(ctl):
    t0 = time.monotonic()
    ctl.move_joints([20, 0, 0, 0, 0, OPEN], speed_dps=100)  # 20 deg at 100 deg/s
    assert time.monotonic() - t0 >= 0.19


def test_speed_is_capped_at_max_joint_speed(bus):
    ctl = ArmController(bus, max_step_deg=10, max_speed_dps=100)
    t0 = time.monotonic()
    ctl.move_joints([20, 0, 0, 0, 0, OPEN], speed_dps=10_000)
    assert time.monotonic() - t0 >= 0.19


def test_move_leaves_the_gripper_where_the_gripper_command_put_it():
    bus = MockBus(object_at=30.0)
    ctl = ArmController(bus, max_speed_dps=1000)
    assert ctl.set_gripper(0) is True
    ctl.move_joints([20, 0, 0, 0, 0, OPEN], speed_dps=1000)  # gripper value copied from an old seed
    assert bus.goal[5] == 0
    assert ctl.holding() is True


@pytest.mark.parametrize("target", [[1, 2, 3], [0, 0, 0, 0, 0, 0, 0], [0, 0, math.nan, 0, 0, OPEN], "abcdef"])
def test_bad_target_is_rejected_without_moving(ctl, bus, target):
    with pytest.raises(RobotdError) as e:
        ctl.move_joints(target, speed_dps=30)
    assert e.value.status == 400
    assert goals(bus) == []


@pytest.mark.parametrize("speed", [0, -5, math.inf])
def test_bad_speed_is_rejected_without_moving(ctl, bus, speed):
    with pytest.raises(RobotdError) as e:
        ctl.move_joints([10, 0, 0, 0, 0, OPEN], speed_dps=speed)
    assert e.value.status == 400
    assert goals(bus) == []


def test_move_with_torque_off_is_refused(ctl, bus):
    ctl.set_torque(False)
    with pytest.raises(RobotdError) as e:
        ctl.move_joints([10, 0, 0, 0, 0, OPEN], speed_dps=30)
    assert e.value.status == 409
    assert bus.present[0] == 0


def test_torque_on_holds_the_pose_the_arm_was_moved_to_by_hand():
    bus = MockBus(torque=False)
    ctl = ArmController(bus)
    bus.present[0] = 12.0  # moved by hand while limp
    ctl.set_torque(True)
    # Goal = Present is written BEFORE the servos are energised, so they never chase the stale goal
    assert bus.log[-2:] == [("goal", [12.0, 0, 0, 0, 0, OPEN]), ("torque", True)]
    assert bus.present[0] == 12.0


def test_stop_interrupts_a_running_move_and_holds_position(bus):
    ctl = ArmController(bus, max_step_deg=10, max_speed_dps=100)
    errors: list[RobotdError] = []

    def run():
        try:
            ctl.move_joints([90, 0, 0, 0, 0, OPEN], speed_dps=100)  # ~0.9 s
        except RobotdError as e:
            errors.append(e)

    th = threading.Thread(target=run)
    th.start()
    time.sleep(0.25)
    t0 = time.monotonic()
    ctl.stop()
    th.join(1.0)
    assert not th.is_alive() and time.monotonic() - t0 < 0.2
    assert [e.status for e in errors] == [409]
    pan = bus.present[0]
    assert 0 < pan < 90
    assert bus.goal[0] == pytest.approx(pan)
    time.sleep(0.15)
    assert bus.present[0] == pytest.approx(pan)


def test_second_motion_while_moving_is_refused(bus):
    ctl = ArmController(bus, max_step_deg=10, max_speed_dps=100)
    th = threading.Thread(target=ctl.move_joints, args=([50, 0, 0, 0, 0, OPEN], 100))
    th.start()
    time.sleep(0.1)
    with pytest.raises(RobotdError) as e:
        ctl.move_joints([0, 10, 0, 0, 0, OPEN], speed_dps=100)
    th.join(1.0)
    assert e.value.status == 409
    assert bus.present[:5] == pytest.approx([50, 0, 0, 0, 0])


def test_move_that_does_not_settle_reports_each_joint_off_target_and_stops_pushing():
    bus = StuckElbowBus()
    ctl = ArmController(bus, max_speed_dps=1000, settle_timeout_s=0.1)
    with pytest.raises(RobotdError) as e:
        ctl.move_joints([0, 0, 5, 0, 0, OPEN], speed_dps=1000)  # one step: too small for the watchdog
    assert e.value.status == 409
    assert e.value.details["errors"] == pytest.approx({"elbow_flex": 5.0})
    assert bus.goal[2] == 0.0  # goal pulled back to where the elbow is, so the servo stops straining


LIMITS = [(-100.0, 100.0)] * 5 + [(0.0, 100.0)]


def test_target_outside_the_joint_limits_is_rejected_without_moving():
    bus = MockBus(limits=LIMITS)
    ctl = ArmController(bus, limit_margin_deg=2.0)
    with pytest.raises(RobotdError) as e:
        ctl.move_joints([99, 0, -120, 0, 0, OPEN], speed_dps=30)
    assert e.value.status == 400
    assert e.value.details["outside"] == {"shoulder_pan": {"target": 99.0, "min": -98.0, "max": 98.0},
                                          "elbow_flex": {"target": -120.0, "min": -98.0, "max": 98.0}}
    assert goals(bus) == []


def test_target_inside_the_margin_moves():
    bus = MockBus(limits=LIMITS)
    ArmController(bus, max_speed_dps=1000, limit_margin_deg=2.0).move_joints([98, 0, -98, 0, 0, OPEN], 1000)
    assert bus.present[:5] == pytest.approx([98, 0, -98, 0, 0])


def test_watchdog_stops_a_joint_that_stops_following_its_goal():
    bus = StuckElbowBus()
    ctl = ArmController(bus, max_step_deg=10, max_speed_dps=1000)
    with pytest.raises(RobotdError) as e:
        ctl.move_joints([0, 0, 40, 0, 0, OPEN], speed_dps=1000)
    assert e.value.status == 409
    assert e.value.details["lag"] == pytest.approx({"elbow_flex": 20.0})
    assert goals(bus, joint=2) == pytest.approx([10, 20, 0])  # two steps, then held where it is


class DropsSecondWriteBus(MockBus):
    """A bus whose second goal write fails, like a USB hiccup mid-move."""

    writes = 0

    def write_goal(self, goal):
        self.writes += 1
        if self.writes == 2:
            raise ConnectionError("Failed to sync write 'Goal_Position'")
        super().write_goal(goal)


def test_bus_error_mid_move_answers_503_and_holds():
    bus = DropsSecondWriteBus()
    ctl = ArmController(bus, max_step_deg=10, max_speed_dps=1000)
    with pytest.raises(RobotdError) as e:
        ctl.move_joints([30, 0, 0, 0, 0, OPEN], speed_dps=1000)
    assert e.value.status == 503
    assert "ConnectionError" in e.value.message
    assert bus.goal[:5] == bus.present[:5] == pytest.approx([10, 0, 0, 0, 0])


def test_gripper_closing_on_an_object_reports_holding():
    bus = MockBus(object_at=30.0)
    ctl = ArmController(bus)
    assert ctl.set_gripper(0) is True
    state = ctl.state()
    assert state["holding"] is True
    assert state["gripper_pos"] == pytest.approx(30.0)


def test_gripper_closing_on_air_is_not_holding(ctl):
    assert ctl.set_gripper(0) is False
    assert ctl.state()["gripper_pos"] == pytest.approx(0.0)


@pytest.mark.parametrize("pct", [-1, 101, math.nan])
def test_gripper_outside_0_to_100_is_rejected(ctl, bus, pct):
    with pytest.raises(RobotdError) as e:
        ctl.set_gripper(pct)
    assert e.value.status == 400
    assert goals(bus, joint=5) == []


def test_state_has_the_contract_fields():
    bus = MockBus(object_at=30.0)
    ctl = ArmController(bus)
    ctl.set_gripper(0)
    state = ctl.state()
    assert set(state) == {"joints", "holding", "gripper_pos", "gripper_load", "torque", "t"}
    assert len(state["joints"]) == 6
    assert state["joints"][5] == state["gripper_pos"]
    assert state["gripper_load"] > 0
    assert state["torque"] is True
    assert abs(state["t"] - time.time()) < 5
