"""WS7.1: the block-tuned stack seed works on blocks and genuinely slips on cups."""

import pytest

from skillsmith.config import REPO_ROOT, load_config
from skillsmith.events import NullBus
from skillsmith.hal.base import Env
from skillsmith.hal.fake import RIM_FRACTION, FakeWorld
from skillsmith.skills.runtime import run_skill
from skillsmith.skills.validator import validate
from skillsmith.types import Scene, SceneObject

cfg = load_config()
SEED = (REPO_ROOT / "skills/library/seeds/stack.py").read_text()
PICK_PLACE = (REPO_ROOT / "skills/library/pick_place.py").read_text()

OBJECTS = {
    "red block": SceneObject("red_block_0", "red block", "red", "block", 0.18, 0.06, 0.0, 0.03, 0.03),
    "green block": SceneObject("green_block_0", "green block", "green", "block", 0.15, 0.0, 0.0, 0.03, 0.03),
    "blue cup": SceneObject("blue_cup_0", "blue cup", "blue", "cup", 0.22, -0.08, 0.0, 0.08, 0.065),
    "red cup": SceneObject("red_cup_0", "red cup", "red", "cup", 0.22, 0.08, 0.0, 0.08, 0.065),
}


def stack(top, bottom, rim_slip=True, source=SEED):
    w = FakeWorld.from_scene(Scene(tuple(OBJECTS.values()), 0.0, "fake"), cfg, rim_slip=rim_slip)
    return run_skill(source, Env("fake", w, w, w), {"top": top, "bottom": bottom}, cfg, NullBus(),
                     skill="stack", library={"pick_place": PICK_PLACE},
                     success={"op": "on_top", "args": [top, bottom]})


def test_seed_passes_static_checks():
    assert validate(SEED) == []


@pytest.mark.parametrize("top,bottom", [("green block", "red block"), ("red block", "green block"),
                                        ("green block", "red cup")])
def test_seed_stacks_blocks_even_with_rim_slip(top, bottom):
    r = stack(top, bottom)
    assert r.ok, r.reason()
    assert r.safety.ok


@pytest.mark.parametrize("bottom", ["red cup", "green block"])
def test_seed_slips_on_a_cup_in_carry(bottom):
    r = stack("blue cup", bottom)
    assert not r.ok
    assert "lost blue cup while carrying it" in r.error
    grasp = next(c for c in r.trace if c.name == "grasp")
    assert grasp.result is True  # it was held...
    last = r.trace[-1]  # ...and lost on the sideways carry to the bottom object
    b = OBJECTS[bottom]
    assert last.name == "move_to" and (last.args["x"], last.args["y"]) == (b.x, b.y)


def test_cup_slip_is_a_sim_to_real_gap():
    r = stack("blue cup", "red cup", rim_slip=False)
    assert r.ok, r.reason()


def test_grip_point_is_below_rim_on_blocks_and_above_it_on_cups():
    depth = 0.012  # GRIP_DEPTH in the seed
    assert "GRIP_DEPTH = 0.012" in SEED
    block, cup = OBJECTS["green block"], OBJECTS["blue cup"]
    assert (block.height - depth) / block.height < RIM_FRACTION
    assert (cup.height - depth) / cup.height > RIM_FRACTION


def test_mid_height_fix_works_on_cups():
    fix = SEED.replace("grip_z = a.top - GRIP_DEPTH", "grip_z = a.z + a.height * 0.5")
    assert validate(fix) == []
    r = stack("blue cup", "red cup", source=fix)
    assert r.ok, r.reason()
