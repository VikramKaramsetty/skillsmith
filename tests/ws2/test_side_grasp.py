"""Side grasp: level-gripper IK, the tapered sim cup and robot.approach_side end to end in the sim."""

from __future__ import annotations

import math

import pytest

from skillsmith.config import load_config
from skillsmith.events import NullBus
from skillsmith.hal.base import make_env
from skillsmith.skills.runtime import run_skill
from skillsmith.types import Scene, SceneObject

pytest.importorskip("mujoco")
pytest.importorskip("placo")

CFG = load_config()
pytestmark = pytest.mark.skipif(not CFG.path(CFG.robot.mjcf).is_file(), reason="SO101 MJCF not vendored (WS2.1)")

from skillsmith.hal.sim import TAPER_SEGMENTS, build_world  # noqa: E402
from skillsmith.kinematics import SIDE_MAX_TILT_DEG, make_kinematics  # noqa: E402

SEED = [0.0, 0.0, 0.0, 90.0, 0.0, 100.0]
MOVE_CUP = '''
SKILL = {"name": "move_cup", "params": {"obj": "str", "x": "float", "y": "float"}, "uses": []}


def run(robot, scene, skills, obj, x, y):
    cup = scene.require(obj)
    carry_z = cup.top + 0.03
    robot.approach_side(obj)
    if not robot.grasp():
        raise SkillError("side grasp missed")
    hx, hy, held_z = robot.position()
    robot.move_to(hx, hy, carry_z)
    robot.move_to(x, y, carry_z)
    robot.move_to(x, y, held_z + 0.003)
    robot.release()
    robot.move_to(x, y, carry_z)
    robot.home()
'''


def _cup(x: float, y: float) -> SceneObject:
    return SceneObject("pink_cup_0", "pink cup", "pink", "cup", x, y, 0.0, 0.0953, 0.0762, top_width=0.0508)


@pytest.fixture(scope="module")
def kin():
    return make_kinematics(CFG)


@pytest.mark.parametrize("target", [(0.27, 0.0, 0.075), (0.28, 0.08, 0.06), (0.30, -0.10, 0.045), (0.24, -0.05, 0.10)])
def test_side_ik_is_level_with_jaws_sideways(kin, target):
    q = kin.ik(target, SEED, approach="side")
    assert q is not None, f"side ik{target} unreachable (tilt {kin.last_tilt_deg})"
    assert math.dist(kin.fk(q), target) < 0.005
    assert kin.last_tilt_deg <= SIDE_MAX_TILT_DEG
    zx, zy, zz = kin.tool_axis(q)
    assert abs(zz) < math.sin(math.radians(SIDE_MAX_TILT_DEG)) + 1e-6       # pointing level...
    assert zx * target[0] + zy * target[1] > 0                              # ...away from the base
    assert abs(kin.tool_axis(q, "x")[2]) < 0.05                             # jaws close horizontally


def test_side_ik_refuses_near_the_base(kin):
    assert kin.ik((0.15, 0.0, 0.06), SEED, approach="side") is None
    assert kin.ik((0.17, 0.0, 0.06), SEED, approach="side") is None  # wrist_flex/elbow out of range


def test_wrist_camera_sits_above_a_level_gripper(kin):
    q = kin.ik((0.27, 0.0, 0.075), SEED, approach="side")
    pose = kin.wrist_camera_pose(q)
    assert pose[2, 3] > 0.075 + 0.03  # camera on top (roll -90), not hanging under the jaws


def test_tapered_cup_is_stacked_cylinders_narrowing_upward():
    import mujoco

    world = build_world(Scene((_cup(0.27, 0.0),), 0.0, "test"), str(CFG.path(CFG.robot.mjcf)))
    body = world.model.body("pink_cup_0").id
    radii = [float(world.model.geom_size[g][0]) for g in range(world.model.ngeom)
             if world.model.geom_bodyid[g] == body and world.model.geom_type[g] == mujoco.mjtGeom.mjGEOM_CYLINDER
             and world.model.geom_contype[g]]  # the painted top disc is visual only
    assert len(radii) == TAPER_SEGMENTS
    assert radii == sorted(radii, reverse=True)
    assert 0.0254 < radii[-1] < 0.030 and 0.035 < radii[0] < 0.0381


@pytest.mark.parametrize("start, goal", [((0.29, 0.02), (0.28, -0.10)), ((0.30, -0.03), (0.28, 0.08))])
def test_move_cup_with_a_side_grasp_in_sim(start, goal):
    env = make_env("sim", CFG, Scene((_cup(*start),), 0.0, "test"))
    try:
        result = run_skill(MOVE_CUP, env, {"obj": "pink cup", "x": goal[0], "y": goal[1]}, CFG, NullBus(),
                           skill="move_cup", success={"op": "near", "args": ["pink cup", *goal]})
    finally:
        env.close()
    assert result.ok, result.error
    names = [c.name for c in result.trace]
    assert names[:2] == ["approach_side", "grasp"]
    if env.arm.wrist_frame() is not None:  # offscreen GL here: the rendered wrist camera found the top
        assert result.trace[0].result < 0.01  # ground truth in sim, so the fix is small


def test_a_cup_near_the_base_is_still_picked_by_a_fallback_approach():
    env = make_env("sim", CFG, Scene((_cup(0.20, -0.15),), 0.0, "test"))
    try:
        result = run_skill(MOVE_CUP, env, {"obj": "pink cup", "x": 0.28, "y": -0.10}, CFG, NullBus(),
                           skill="move_cup", success={"op": "near", "args": ["pink cup", 0.28, -0.10]})
    finally:
        env.close()
    assert result.ok, result.error


def test_a_cup_against_the_base_is_unreachable_before_moving():
    env = make_env("sim", CFG, Scene((_cup(0.12, 0.0),), 0.0, "test"))
    try:
        result = run_skill(MOVE_CUP, env, {"obj": "pink cup", "x": 0.28, "y": -0.10}, CFG, NullBus(),
                           skill="move_cup", success={"op": "near", "args": ["pink cup", 0.28, -0.10]})
    finally:
        env.close()
    assert not result.ok
    assert result.error.startswith(("Unreachable", "SafetyViolation")), result.error
    assert result.trace[0].ee_before == result.trace[0].ee_after or result.trace[0].name == "approach_side"
