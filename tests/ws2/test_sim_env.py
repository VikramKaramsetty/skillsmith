"""WS2.4: make_sim_env; unintended contacts raise CollisionError and show up in safety_issues()."""

from __future__ import annotations

import math

import pytest

from skillsmith.config import load_config
from skillsmith.events import EventBus
from skillsmith.hal.base import make_env
from skillsmith.hal.fake import FakeWorld
from skillsmith.skills.runtime import run_skill
from skillsmith.types import CollisionError, SkillError

pytest.importorskip("mujoco")
pytest.importorskip("placo")

CFG = load_config()
pytestmark = pytest.mark.skipif(not CFG.path(CFG.robot.mjcf).is_file(), reason="SO101 MJCF not vendored (WS2.1)")

STEP = 0.02  # m; vertical moves in steps, like Robot._waypoints

# A pick-and-place whose grasp point is shifted by (dx, dy) so the object sits between the jaws
# (the kinematic tip is the fixed jaw's tip, see hal/sim.py). The fake world uses dx = dy = 0.
PLACE_SKILL = '''
SKILL = {"name": "place_test", "params": {"obj": "str", "x": "float", "y": "float", "place_z": "float"}}


def run(robot, scene, skills, obj, x, y, place_z, dx=0.0, dy=0.0):
    o = scene.require(obj)
    grip = o.height * 0.5
    robot.release()
    robot.move_to(o.x + dx, o.y + dy, 0.12)
    robot.move_to(o.x + dx, o.y + dy, o.z + grip)
    if not robot.grasp():
        raise SkillError("grasp missed")
    robot.move_to(o.x + dx, o.y + dy, 0.15)
    robot.move_to(x + dx, y + dy, 0.15)
    robot.move_to(x + dx, y + dy, place_z + grip + 0.005)
    robot.release()
    robot.move_to(x + dx, y + dy, 0.15)
'''


class _Bus(EventBus):
    def __init__(self):
        self.events = []

    def emit(self, type: str, **data) -> None:
        self.events.append((type, data))


@pytest.fixture(scope="module")
def env():
    e = make_env("sim", CFG)
    yield e
    e.close()


@pytest.fixture(autouse=True)
def _fresh(env):
    env.arm.reset_scene()


def _tip(env, xyz):
    q = env.kin.ik(xyz, env.arm.joints())
    assert q is not None, f"ik{xyz} unreachable"
    env.arm.move_joints(q, 45)


def _go(env, xyz):
    """Tip to xyz: horizontal at the current height first, then straight down/up in STEP chunks."""
    x0, y0, z0 = env.kin.fk(env.arm.joints())
    n = max(1, math.ceil(abs(xyz[2] - z0) / STEP))
    for k in range(1, n + 1):
        _tip(env, (xyz[0], xyz[1], z0 + (xyz[2] - z0) * k / n))


def _jaw_offset(env):
    """(dx, dy) from the kinematic tip to the centre between the jaws, robot frame, at the current pose."""
    centre = env.arm.world.to_robot(tuple(float(v) for v in env.arm.data.site("gripperframe").xpos))
    tip = env.kin.fk(env.arm.joints())
    return centre[0] - tip[0], centre[1] - tip[1]


def _over(env, obj, z):
    """Hover with the jaw centre (not the fixed jaw) over obj at tip height z."""
    _tip(env, (obj.x, obj.y, z))
    dx, dy = _jaw_offset(env)
    _tip(env, (obj.x - dx, obj.y - dy, z))
    return obj.x - dx, obj.y - dy


def test_env_starts_at_home_with_the_scene():
    env = make_env("sim", CFG)
    assert env.name == "sim" and env.arm.name == "sim" and env.perception is env.arm
    assert env.kin.fk(env.arm.joints()) == pytest.approx(tuple(CFG.robot.home_xyz), abs=0.005)
    q = env.arm.joints()
    assert len(q) == 6 and q[5] == pytest.approx(100.0)
    seen = env.perception.observe()
    assert seen.source == "sim"
    for obj in FakeWorld.demo(CFG).observe().objects:
        got = seen.find(obj.id)
        assert got.xy_dist(obj) < 1e-3 and abs(got.z - obj.z) < 1e-3
    assert env.arm.safety_issues() == [] and env.arm.gripper_holding() is False
    assert env.perception.snapshot("x.png") is None  # frames and video arrive with WS2.5


def test_free_moves_reach_their_targets_without_issues(env):
    for p in ((0.20, 0.0, 0.15), (0.30, 0.0, 0.15), (0.30, 0.0, 0.03), (0.30, 0.0, 0.15)):
        _go(env, p)
        assert env.kin.fk(env.arm.joints()) == pytest.approx(p, abs=0.004)
    env.arm.set_gripper(0)
    env.arm.set_gripper(100)
    assert env.arm.safety_issues() == [] and not env.arm.gripper_holding()


def test_tip_on_object_centre_drives_the_fixed_jaw_into_it(env):
    block = env.perception.observe().require("green block")
    with pytest.raises(CollisionError, match="green block") as err:
        _go(env, (block.x, block.y, block.z + block.height / 2))
    assert "z=" in str(err.value) and "spans z=0.000..0.030" in str(err.value)
    issues = env.arm.safety_issues()
    assert [i.code for i in issues] == ["COLLISION"] and "green block" in issues[0].message
    env.arm.reset_scene()
    assert env.arm.safety_issues() == []


def test_closed_gripper_pushed_into_a_cup_raises(env):
    cup = env.perception.observe().require("red cup")
    _go(env, (cup.x, cup.y, cup.top + 0.05))
    env.arm.set_gripper(0)
    with pytest.raises(CollisionError, match="red cup"):
        _go(env, (cup.x, cup.y, cup.z + cup.height / 2))


def test_pushing_into_the_table_raises(env):
    _go(env, (0.28, -0.15, 0.02))
    q = env.arm.joints()
    q[1] += 15.0  # shoulder down, through the table top (the guard would refuse it; the sim must not allow it)
    with pytest.raises(CollisionError, match="the table"):
        env.arm.move_joints(q, 30)
    assert env.arm.safety_issues()[0].code == "COLLISION"


def test_joint_target_past_a_limit_raises(env):
    q = env.arm.joints()
    q[0] = 150.0
    with pytest.raises(SkillError, match="shoulder_pan"):
        env.arm.move_joints(q, 30)


def test_grasp_carry_and_release_moves_the_object(env):
    block = env.perception.observe().require("green block")
    _go(env, (block.x, block.y, 0.10))
    gx, gy = _over(env, block, 0.10)
    _go(env, (gx, gy, block.z + block.height / 2))
    env.arm.set_gripper(0)
    assert env.arm.gripper_holding()
    _go(env, (gx, gy, 0.12))
    lifted = env.perception.observe().require("green block")
    assert lifted.z == pytest.approx(0.12 - block.height / 2, abs=0.01)
    move = 0.16  # to x ~ 0.31: clear of both cups, including the wrist camera ~6 cm to the jaws' side
    _go(env, (gx, gy, 0.16))
    _go(env, (gx + move, gy, 0.16))
    _go(env, (gx + move, gy, block.height / 2 + 0.005))
    env.arm.set_gripper(100)
    assert not env.arm.gripper_holding()
    _go(env, (gx + move, gy, 0.16))
    placed = env.perception.observe().require("green block")
    assert (placed.x, placed.y) == pytest.approx((block.x + move, block.y), abs=0.01)
    assert placed.z == pytest.approx(0.0, abs=0.003)
    assert env.arm.safety_issues() == []


def _offset_for(env, obj):
    _go(env, (obj.x, obj.y, 0.10))
    dx, dy = _jaw_offset(env)
    env.arm.reset_scene()
    return -dx, -dy


def _place(e, place_z: float, **offset):
    bus = _Bus()
    cup = e.perception.observe().require("red cup")
    params = {"obj": "green block", "x": cup.x, "y": cup.y, "place_z": place_z, **offset}
    result = run_skill(PLACE_SKILL, e, params, CFG, bus, skill="place_test",
                       success={"op": "on_top", "args": ["green block", "red cup"]})
    return result


def test_block_stacks_on_cup_in_sim(env):
    dx, dy = _offset_for(env, env.perception.observe().require("green block"))
    result = _place(env, 0.082, dx=dx, dy=dy)
    assert result.ok, (result.error, result.verify, result.safety)


@pytest.mark.parametrize("backend", ["fake", "sim"])
def test_lowered_place_height_raises_collision_like_the_fake_world(env, backend):
    """Lowering the held block 2 cm into the cup top is a collision in both worlds."""
    offset = {}
    if backend == "sim":
        dx, dy = _offset_for(env, env.perception.observe().require("green block"))
        offset = {"dx": dx, "dy": dy}
    e = env if backend == "sim" else make_env("fake", CFG)
    result = _place(e, 0.06, **offset)
    assert not result.ok and result.error.startswith("CollisionError"), result.error
    assert "red cup" in result.error
    assert any(i.code == "COLLISION" for i in result.safety.issues)
    if backend == "sim":
        assert "held green block hit red cup" in result.error and "mm deep" in result.error
        assert any(i.message.startswith("sim contact:") for i in result.safety.issues)
