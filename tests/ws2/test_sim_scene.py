"""WS2.3: the MuJoCo scene builder turns Scene objects into cups and blocks on a table."""

from __future__ import annotations

import pytest

from skillsmith.config import load_config
from skillsmith.hal.fake import FakeWorld
from skillsmith.hal.sim import TABLE_NAME, build_world
from skillsmith.types import Scene, SceneObject

mujoco = pytest.importorskip("mujoco")

CFG = load_config()
MJCF = CFG.path(CFG.robot.mjcf)
pytestmark = pytest.mark.skipif(not MJCF.is_file(), reason="SO101 MJCF not vendored (WS2.1)")


def _scene(*extra: SceneObject) -> Scene:
    demo = FakeWorld.demo(CFG).observe()
    return Scene(objects=demo.objects + extra, t=0.0, source="fake")


YELLOW_ON_RED = SceneObject("yellow_block_0", "yellow block", "yellow", "block", 0.22, 0.08, 0.08, 0.03, 0.03)


def _settle(world, seconds: float = 1.0):
    data = world.make_data()
    for _ in range(int(seconds / world.model.opt.timestep)):
        mujoco.mj_step(world.model, data)
    return data


def _contact_pairs(world, data) -> set:
    names = set()
    for c in data.contact[:data.ncon]:
        a, b = (mujoco.mj_id2name(world.model, mujoco.mjtObj.mjOBJ_GEOM, g) for g in (c.geom1, c.geom2))
        names.add(frozenset((a, b)))
    return names


def test_objects_become_cylinders_and_boxes_with_scene_sizes():
    world = build_world(_scene(), MJCF)
    m = world.model
    cup, block = m.geom("blue_cup_0"), m.geom("green_block_0")
    assert cup.type[0] == mujoco.mjtGeom.mjGEOM_CYLINDER
    assert cup.size[:2] == pytest.approx([0.0325, 0.04])       # radius = width/2, half height
    assert block.type[0] == mujoco.mjtGeom.mjGEOM_BOX
    assert block.size == pytest.approx([0.015, 0.015, 0.015])
    assert m.body("blue_cup_0").jntnum[0] == 1                   # free body: it can be picked up
    assert m.geom(TABLE_NAME).size[2] > 0


def test_origin_is_pan_axis_on_the_table():
    world = build_world(None, MJCF)
    data = world.make_data()
    pan = data.joint("shoulder_pan").xanchor
    assert world.origin == pytest.approx((pan[0], pan[1], 0.0))
    table_top = data.geom(TABLE_NAME).xpos[2] + world.model.geom(TABLE_NAME).size[2]
    assert table_top == pytest.approx(0.0, abs=1e-9)
    assert world.objects == {}


def test_initial_pose_matches_scene_in_robot_frame():
    scene = _scene(YELLOW_ON_RED)
    world = build_world(scene, MJCF)
    seen = {o.id: o for o in world.observe(world.make_data()).objects}
    for obj in scene.objects:
        got = seen[obj.id]
        assert (got.x, got.y, got.z) == pytest.approx((obj.x, obj.y, obj.z), abs=1e-6)
        assert (got.label, got.kind, got.height, got.width) == (obj.label, obj.kind, obj.height, obj.width)


def test_objects_settle_in_place_and_stack_rests_on_cup():
    world = build_world(_scene(YELLOW_ON_RED), MJCF)
    data = _settle(world)
    after = world.observe(data, t=1.0)
    assert after.source == "sim" and after.t == 1.0
    for obj in _scene(YELLOW_ON_RED).objects:
        got = after.find(obj.id)
        assert got.xy_dist(obj) < 0.002 and abs(got.z - obj.z) < 0.002, got
    pairs = _contact_pairs(world, data)
    assert frozenset(("yellow_block_0", "red_cup_0")) in pairs
    assert frozenset(("blue_cup_0", TABLE_NAME)) in pairs
    touched = {n for p in pairs for n in p}
    assert touched <= {TABLE_NAME, *world.objects}, "the arm at its start pose touches nothing"


def test_ball_and_unknown_kinds_and_colors():
    ball = SceneObject("ball_0", "orange ball", "orange", "ball", 0.18, -0.1, 0.0, 0.04, 0.04)
    odd = SceneObject("thing_0", "teal thing", "teal", "widget", 0.25, 0.0, 0.0, 0.02, 0.05)
    world = build_world(Scene(objects=(ball, odd), t=0.0, source="fake"), MJCF)
    assert world.model.geom("ball_0").type[0] == mujoco.mjtGeom.mjGEOM_SPHERE
    assert world.model.geom("thing_0").type[0] == mujoco.mjtGeom.mjGEOM_BOX
    seen = world.observe(world.make_data())
    assert seen.find("ball_0").z == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize("bad", [
    (SceneObject("a", "a", "red", "cup", 0.2, 0.0, 0.0, 0.08, 0.06),) * 2,
    (SceneObject("flat", "flat", "red", "block", 0.2, 0.0, 0.0, 0.0, 0.03),),
])
def test_bad_scenes_raise(bad):
    with pytest.raises(ValueError):
        build_world(Scene(objects=bad, t=0.0, source="fake"), MJCF)
