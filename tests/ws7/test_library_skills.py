"""WS7.2: put_in, tower and sort_by_color pass static checks and run on the fake world.

The fake world models cups as solid, so a dropped object comes to rest on the rim rather
than inside. These tests check what the fake world can show: the object ends centred over
the container (inside its opening) and above its rim. `inside` itself needs sim or real.
"""

import ast

import pytest

from skillsmith.config import REPO_ROOT, load_config
from skillsmith.events import NullBus
from skillsmith.hal.base import Env
from skillsmith.hal.fake import FakeWorld
from skillsmith.skills.runtime import run_skill
from skillsmith.skills.validator import validate
from skillsmith.types import Scene, SceneObject

cfg = load_config()
LIB = REPO_ROOT / "skills/library"
SOURCES = {p.stem: p.read_text() for p in LIB.glob("*.py")}
SOURCES["stack"] = (LIB / "seeds/stack.py").read_text()  # the demo seed until Droid saves a fix
NEW = ("put_in", "tower", "sort_by_color")


def obj(color, kind, x, y, height=0.03, width=0.03):
    return SceneObject(f"{color}_{kind}_0", f"{color} {kind}", color, kind, x, y, 0.0, height, width)


BLOCKS = (obj("red", "block", 0.15, 0.08), obj("green", "block", 0.15, 0.0), obj("blue", "block", 0.15, -0.08))
CUPS = (obj("red", "cup", 0.25, 0.10, 0.08, 0.065), obj("green", "cup", 0.27, 0.0, 0.08, 0.065),
        obj("blue", "cup", 0.25, -0.10, 0.08, 0.065))


def run(skill, params, objects, success=None, rim_slip=True):
    w = FakeWorld.from_scene(Scene(tuple(objects), 0.0, "fake"), cfg, rim_slip=rim_slip)
    return run_skill(SOURCES[skill], Env("fake", w, w, w), params, cfg, NullBus(),
                     skill=skill, library=SOURCES, success=success)


def assert_dropped_into(scene, item, container):
    a, c = scene.find(item), scene.find(container)
    assert a.xy_dist(c) < c.width / 2, f"{a.label} is {a.xy_dist(c) * 100:.1f} cm off {c.label}"
    assert a.z >= c.z


@pytest.mark.parametrize("name", NEW)
def test_library_skill_passes_static_checks(name):
    assert validate(SOURCES[name]) == []


@pytest.mark.parametrize("name", NEW)
def test_uses_lists_every_called_skill(name):
    src = SOURCES[name]
    called = {u for u in SOURCES if f'skills.call("{u}"' in src}
    skill = next(ast.literal_eval(n.value) for n in ast.parse(src).body
                 if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "SKILL")
    assert set(skill["uses"]) == called


def test_put_in_drops_block_over_cup():
    r = run("put_in", {"obj": "blue block", "container": "blue cup"}, BLOCKS + CUPS)
    assert r.ok, r.reason()
    assert r.safety.ok
    assert_dropped_into(r.scene_after, "blue block", "blue cup")
    assert [c.name for c in r.trace].count("align_over") == 2


def test_put_in_refuses_object_wider_than_container():
    r = run("put_in", {"obj": "red cup", "container": "green block"}, BLOCKS + CUPS)
    assert not r.ok
    assert "too wide" in r.error
    assert not any(c.name == "move_to" for c in r.trace)


def test_tower_of_three_blocks():
    order = ["red block", "green block", "blue block"]
    success = {"op": "all", "args": [{"op": "on_top", "args": [order[1], order[0]]},
                                     {"op": "on_top", "args": [order[2], order[1]]}]}
    r = run("tower", {"objs": order}, BLOCKS, success=success)
    assert r.ok, r.reason()
    assert r.safety.ok
    zs = [r.scene_after.find(n).z for n in order]
    assert zs == sorted(zs) and zs[0] < zs[1] < zs[2]  # three levels, bottom first


def test_tower_reuses_stack_so_the_cup_slip_carries_over():
    r = run("tower", {"objs": ["red block", "blue cup"]}, BLOCKS + CUPS)
    assert not r.ok
    assert "lost blue cup while carrying it" in r.error
    assert "stack.py" in r.error


def test_tower_needs_two_objects():
    r = run("tower", {"objs": ["red block"]}, BLOCKS)
    assert not r.ok
    assert "at least 2" in r.error


def test_sort_by_color_puts_each_block_over_its_cup():
    r = run("sort_by_color", {}, BLOCKS + CUPS)
    assert r.ok, r.reason()
    assert r.safety.ok
    for color in ("red", "green", "blue"):
        assert_dropped_into(r.scene_after, f"{color} block", f"{color} cup")


def test_sort_by_color_checks_every_match_before_moving():
    r = run("sort_by_color", {}, BLOCKS + CUPS[:2])
    assert not r.ok
    assert "no blue cup for blue block" in r.error
    assert not any(c.name == "move_to" for c in r.trace)
