"""WS7.4: pyramid passes static checks, builds widest-first on the fake world and reuses stack."""

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
FIXED_STACK = SOURCES["stack"].replace("grip_z = a.top - GRIP_DEPTH", "grip_z = a.z + a.height * 0.5")


def obj(color, kind, x, y, height, width):
    return SceneObject(f"{color}_{kind}_0", f"{color} {kind}", color, kind, x, y, 0.0, height, width)


# deliberately listed out of order: the skill must sort them widest first
BIG = obj("red", "block", 0.15, 0.08, 0.03, 0.06)
MID = obj("green", "block", 0.15, 0.0, 0.03, 0.045)
SMALL = obj("blue", "block", 0.15, -0.08, 0.03, 0.03)
CUP = obj("blue", "cup", 0.25, -0.10, 0.08, 0.05)
SAME = obj("yellow", "block", 0.22, 0.10, 0.03, 0.03)
ORDER = ["red block", "green block", "blue block"]
PYRAMID = {"op": "all", "args": [{"op": "on_top", "args": ["green block", "red block"]},
                                 {"op": "on_top", "args": ["blue block", "green block"]}]}


def run(params, objects, success=None, library=SOURCES):
    w = FakeWorld.from_scene(Scene(tuple(objects), 0.0, "fake"), cfg, rim_slip=True)
    return run_skill(SOURCES["pyramid"], Env("fake", w, w, w), params, cfg, NullBus(),
                     skill="pyramid", library=library, success=success)


def test_pyramid_passes_static_checks():
    assert validate(SOURCES["pyramid"]) == []


def test_pyramid_builds_widest_first_whatever_the_order_given():
    r = run({"objs": ["blue block", "red block", "green block"]}, (SMALL, BIG, MID), success=PYRAMID)
    assert r.ok, r.reason()
    assert r.safety.ok
    zs = [r.scene_after.find(n).z for n in ORDER]
    assert zs[0] < zs[1] < zs[2]
    assert [c.name for c in r.trace].count("look") >= 2  # re-looks before every level


def test_pyramid_reuses_stack_so_the_cup_slip_carries_over():
    r = run({"objs": ["red block", "blue cup"]}, (BIG, CUP))
    assert not r.ok
    assert "lost blue cup while carrying it" in r.error
    assert "stack.py" in r.error


def test_pyramid_picks_up_a_fixed_stack():
    library = dict(SOURCES, stack=FIXED_STACK)
    success = {"op": "on_top", "args": ["blue cup", "red block"]}
    r = run({"objs": ["red block", "blue cup"]}, (BIG, CUP), success=success, library=library)
    assert r.ok, r.reason()


def test_pyramid_refuses_equal_widths_before_moving():
    r = run({"objs": ["blue block", "yellow block"]}, (SMALL, SAME))
    assert not r.ok
    assert "narrower than the one below" in r.error
    assert not any(c.name == "move_to" for c in r.trace)


def test_pyramid_needs_two_objects():
    r = run({"objs": ["red block"]}, (BIG,))
    assert not r.ok
    assert "at least 2" in r.error
