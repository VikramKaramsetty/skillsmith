"""WS4.8: task parser templates (sort, tower, put-in) and the fallback hook to Droid's draft path."""

import pytest

from skillsmith.config import load_config
from skillsmith.hal.fake import FakeWorld
from skillsmith.tasks import get_fallback, match_template, parse_task, set_fallback, spec_from_dict
from skillsmith.types import Scene, SceneObject, TaskSpec
from skillsmith.verify import evaluate


@pytest.fixture(autouse=True)
def _no_global_fallback():
    previous = set_fallback(None)
    yield
    set_fallback(previous)


# ---- put-in ---------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "put the red block in the green cup",
    "Place the red block into the green cup.",
    "drop red block inside the green cup",
    "move the red block into the green cup",
    "insert the red block into green cup!",
])
def test_put_in_phrasings(text):
    t = parse_task(text)
    assert (t.skill, t.params) == ("put_in", {"obj": "red block", "container": "green cup"})
    assert t.success == {"op": "inside", "args": ["red block", "green cup"]}
    assert t.text == text.strip()


def test_in_front_of_is_not_a_container():
    assert match_template("put the red block in front of the green cup") is None


# ---- tower ----------------------------------------------------------------------------------

TOWER3 = ["red block", "green block", "blue block"]
TOWER3_SUCCESS = {"op": "all", "args": [{"op": "on_top", "args": ["green block", "red block"]},
                                        {"op": "on_top", "args": ["blue block", "green block"]}]}


@pytest.mark.parametrize("text", [
    "build a tower of the red block, the green block and the blue block",
    "Build a tower with red block, green block, and blue block.",
    "make a tower from the red block then the green block then the blue block",
    "build a tower of the red block, the green block and the blue block, bottom first",
    "stack the red block, the green block and the blue block into a tower",
    "stack the blue block on the green block on the red block",
    "stack the blue block on top of the green block on top of the red block",
])
def test_tower_phrasings_bottom_first(text):
    t = parse_task(text)
    assert (t.skill, t.params) == ("tower", {"objs": TOWER3})
    assert t.success == TOWER3_SUCCESS


def test_two_level_tower_success_is_a_single_on_top():
    t = parse_task("build a tower of the red cup and the blue cup")
    assert t.params == {"objs": ["red cup", "blue cup"]}
    assert t.success == {"op": "on_top", "args": ["blue cup", "red cup"]}


@pytest.mark.parametrize("text", [
    "build a tower of the red block",                           # one level is not a tower
    "build a tower of the red block and the red block",         # same object twice
    "build a tower of a, b, c, d, e, f and g",                  # taller than tower.py allows
])
def test_bad_towers_do_not_match(text):
    assert match_template(text) is None


def test_plain_stack_still_parses_as_stack():
    t = parse_task("Stack the blue cup on the red cup.")
    assert (t.skill, t.params) == ("stack", {"top": "blue cup", "bottom": "red cup"})
    assert t.success == {"op": "on_top", "args": ["blue cup", "red cup"]}


def test_tower_success_evaluates_with_todays_verify():
    def at(color, z):
        return SceneObject(f"{color}_block_0", f"{color} block", color, "block", 0.2, 0.0, z, 0.03, 0.03)

    built = Scene((at("red", 0.0), at("green", 0.03), at("blue", 0.06)), 0.0, "fake")
    t = parse_task("build a tower of the red block, the green block and the blue block")
    assert evaluate(t.success, built).ok
    flat = Scene((at("red", 0.0), at("green", 0.03), at("blue", 0.0)), 0.0, "fake")
    r = evaluate(t.success, flat)
    assert not r.ok and "blue block" in r.detail


# ---- sort -----------------------------------------------------------------------------------

SORT = ("sort_by_color", {"kind": "block", "into": "cup"}, {"op": "sorted_by_color", "args": ["block", "cup"]})


@pytest.mark.parametrize("text", [
    "sort the blocks into the cups by colour",
    "Sort the blocks by color into the cups.",
    "sort by colour",
    "sort the blocks by color",
    "sort all the blocks into the matching cups",
    "sort blocks into cups",
    "put each block in the cup of the same colour",
    "put every block into the matching cup",
    "place all blocks into the same-coloured cup",
    "drop each block in the cup that matches its colour",
])
def test_sort_phrasings(text):
    t = parse_task(text)
    assert (t.skill, t.params, t.success) == SORT


def test_sort_other_kinds_singularised():
    t = parse_task("sort the balls into the boxes by colour")
    assert t.params == {"kind": "ball", "into": "box"}
    assert t.success == {"op": "sorted_by_color", "args": ["ball", "box"]}


@pytest.mark.parametrize("text", ["sort", "sort the red blocks into the cups", "sort the blocks into the big cups"])
def test_unclear_sorts_do_not_match(text):
    assert match_template(text) is None


def test_sort_params_drive_the_library_skill_on_the_fake_world():
    from skillsmith.config import REPO_ROOT
    from skillsmith.events import NullBus
    from skillsmith.hal.base import Env
    from skillsmith.skills.runtime import run_skill

    def obj(color, kind, x, y, h=0.03, w=0.03):
        return SceneObject(f"{color}_{kind}_0", f"{color} {kind}", color, kind, x, y, 0.0, h, w)

    cfg = load_config()
    objects = (obj("red", "block", 0.15, 0.08), obj("blue", "block", 0.15, -0.08),
               obj("red", "cup", 0.25, 0.10, 0.08, 0.065), obj("blue", "cup", 0.25, -0.10, 0.08, 0.065))
    lib = REPO_ROOT / "skills/library"
    sources = {p.stem: p.read_text() for p in lib.glob("*.py")}
    t = parse_task("sort the blocks into the cups by colour")
    w = FakeWorld.from_scene(Scene(objects, 0.0, "fake"), cfg)
    r = run_skill(sources[t.skill], Env("fake", w, w, w), t.params, cfg, NullBus(), skill=t.skill, library=sources)
    assert r.error is None, r.error


# ---- move to a point (unchanged) ------------------------------------------------------------

def test_move_to_point():
    t = parse_task("move the red block to 0.2, -0.1")
    assert t.skill == "pick_place" and t.success == {"op": "near", "args": ["red block", 0.2, -0.1]}


# ---- fallback hook --------------------------------------------------------------------------

NEW_TASK = "wave at the red block"
DRAFTED = {"skill": "wave_at", "params": {"obj": "red block"}, "success": {"op": "near", "args": ["red block", 0.2, 0.0]}}


def test_unknown_task_without_fallback_raises():
    with pytest.raises(ValueError, match="pass --skill"):
        parse_task(NEW_TASK)


def test_fallback_argument_gives_a_new_skill_for_the_draft_path():
    seen = []

    def fallback(text):
        seen.append(text)
        return DRAFTED

    t = parse_task(f"  {NEW_TASK} ", fallback=fallback)
    assert seen == [f"  {NEW_TASK} "]
    assert t == TaskSpec(NEW_TASK, "wave_at", DRAFTED["params"], DRAFTED["success"])


def test_templates_win_over_the_fallback():
    def fallback(text):
        raise AssertionError("fallback must not be called for a template task")

    assert parse_task("put the red block in the green cup", fallback=fallback).skill == "put_in"


def test_global_fallback_and_restore():
    hook = lambda text: TaskSpec("ignored", "wave_at", {}, {"op": "near", "args": ["x", 0, 0]})  # noqa: E731
    assert set_fallback(hook) is None
    assert get_fallback() is hook
    t = parse_task(NEW_TASK)
    assert (t.text, t.skill) == (NEW_TASK, "wave_at")  # text is always the user's, not the hook's
    assert set_fallback(None) is hook
    with pytest.raises(ValueError):
        parse_task(NEW_TASK)


def test_fallback_declining_raises():
    with pytest.raises(ValueError, match="pass --skill"):
        parse_task(NEW_TASK, fallback=lambda text: None)


def test_fallback_exception_becomes_value_error():
    def broken(text):
        raise TimeoutError("droid took too long")

    with pytest.raises(ValueError, match="fallback failed.*TimeoutError.*droid took too long"):
        parse_task(NEW_TASK, fallback=broken)


@pytest.mark.parametrize("bad, msg", [
    ("not a dict", "expected a TaskSpec or dict"),
    ({"skill": "../../etc/passwd", "success": {"op": "near"}}, "skill name"),
    ({"skill": "Wave At", "success": {"op": "near"}}, "skill name"),
    ({"skill": "", "success": {"op": "near"}}, "skill name"),
    ({"skill": "wave", "params": ["obj"], "success": {"op": "near"}}, "params"),
    ({"skill": "wave", "params": {}}, "success"),
    ({"skill": "wave", "params": {}, "success": {"args": []}}, "success"),
])
def test_fallback_answers_are_checked(bad, msg):
    with pytest.raises(ValueError, match=msg):
        parse_task(NEW_TASK, fallback=lambda text: bad)


def test_spec_from_dict_copies_and_defaults_params():
    success = {"op": "near", "args": ["a", 0.1, 0.1]}
    t = spec_from_dict(" do it ", {"skill": "do_it", "success": success})
    assert t == TaskSpec("do it", "do_it", {}, success)
    assert t.success is not success


WAVE = """SKILL = {"name": "wave_at", "params": {"obj": "str"}, "uses": []}


def run(robot, scene, skills, obj):
    a = scene.require(obj)
    robot.move_to(a.x, a.y, 0.15)
    robot.home()
"""


def test_fallback_task_goes_down_the_draft_path():
    """The repair loop drafts when the library lacks task.skill, and a fallback skill is new."""
    from skillsmith.demo import temp_library
    from skillsmith.events import NullBus
    from skillsmith.hal.base import Env, make_env
    from skillsmith.repair.approvers import AutoApprover
    from skillsmith.repair.loop import RepairLoop
    from skillsmith.repair.scripted import ScriptedAuthor
    from skillsmith.skills.library import SkillLibrary

    cfg = load_config()
    spec = {"skill": "wave_at", "params": {"obj": "red cup"},
            "success": {"op": "not", "args": [{"op": "near", "args": ["red cup", 9.0, 9.0]}]}}
    task = parse_task("wave at the red cup", fallback=lambda text: spec)
    w, bus, library = FakeWorld.demo(cfg), NullBus(), SkillLibrary(temp_library())
    assert not library.has(task.skill)
    loop = RepairLoop(cfg, Env("fake", w, w, w), lambda s: make_env("fake", cfg, s), library,
                      ScriptedAuthor([WAVE], []), AutoApprover("fake"), bus)
    result = loop.run(task)
    plan = next(e["data"] for e in bus.events if e["type"] == "plan.selected")
    assert plan["skill"] == "wave_at" and plan["source"] == "draft"
    assert result.ok and library.has("wave_at")
