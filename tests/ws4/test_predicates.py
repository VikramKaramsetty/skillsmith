"""WS4.7: tower, sorted_by_color, left_of and upright predicates with readable details.

Each failure detail names the objects and carries the numbers (cm, z, limits), because it goes
straight into Droid's failure report and onto the dashboard.
"""

from skillsmith.config import REPO_ROOT, load_config
from skillsmith.events import NullBus
from skillsmith.hal.base import Env
from skillsmith.hal.fake import FakeWorld
from skillsmith.skills.runtime import run_skill
from skillsmith.types import Scene, SceneObject
from skillsmith.verify import evaluate

cfg = load_config()


def obj(color, kind, x, y, z=0.0, height=0.03, width=0.03, visible=True):
    return SceneObject(f"{color}_{kind}_0", f"{color} {kind}", color, kind, x, y, z, height, width,
                       visible=visible)


def scene(*objs):
    return Scene(tuple(objs), 0.0, "test")


def check(op, args, *objs):
    return evaluate({"op": op, "args": args}, scene(*objs))


RED_B = obj("red", "block", 0.20, 0.0)
GREEN_B = obj("green", "block", 0.20, 0.0, z=0.03)
BLUE_B = obj("blue", "block", 0.20, 0.0, z=0.06)
TOWER = ["red block", "green block", "blue block"]


# --- tower -------------------------------------------------------------------------------


def test_tower_of_three_passes_with_numbers():
    r = check("tower", TOWER, RED_B, GREEN_B, BLUE_B)
    assert r.ok, r.detail
    assert "tower of 3 stands" in r.detail
    assert "red block / green block / blue block" in r.detail
    assert "top at z=0.090" in r.detail


def test_tower_accepts_a_nested_list():
    assert check("tower", [TOWER], RED_B, GREEN_B, BLUE_B).ok


def test_tower_reports_the_level_that_slid_off():
    slid = obj("blue", "block", 0.20, -0.041, z=0.0)
    r = check("tower", TOWER, RED_B, GREEN_B, slid)
    assert not r.ok
    assert "broken at level 3" in r.detail
    assert "blue block is 4.1 cm from green block in xy (limit 3.0 cm)" in r.detail
    assert "level 2" not in r.detail


def test_tower_reports_an_object_that_is_beside_not_on():
    low = obj("green", "block", 0.20, 0.01, z=0.0)  # xy close, but sitting on the table
    r = check("tower", TOWER[:2], RED_B, low)
    assert not r.ok
    assert "level 2: green block base z=0.000 is below red block top z=0.030" in r.detail


def test_tower_lists_every_broken_level():
    g = obj("green", "block", 0.20, 0.05)
    b = obj("blue", "block", 0.20, -0.05)
    r = check("tower", TOWER, RED_B, g, b)
    assert "level 2:" in r.detail and "level 3:" in r.detail


def test_tower_needs_two_objects_and_visible_ones():
    r = check("tower", ["red block"], RED_B)
    assert not r.ok and "at least 2" in r.detail
    r = check("tower", TOWER, RED_B, GREEN_B)
    assert not r.ok and "cannot see 'blue block'" in r.detail


def test_tower_rejects_two_names_for_one_object():
    r = check("tower", ["red block", "red"], RED_B, GREEN_B)
    assert not r.ok and "both are red block" in r.detail


# --- sorted_by_color ---------------------------------------------------------------------


def cup(color, x, y):
    return obj(color, "cup", x, y, height=0.08, width=0.065)


CUPS = (cup("red", 0.25, 0.10), cup("green", 0.27, 0.0), cup("blue", 0.25, -0.10))


def block_in(color, c, dx=0.0, z=0.02):
    return obj(color, "block", c.x + dx, c.y, z=z)


def test_sorted_passes_when_each_block_is_in_its_cup():
    blocks = [block_in(c.color, c) for c in CUPS]
    r = check("sorted_by_color", [], *blocks, *CUPS)
    assert r.ok, r.detail
    assert r.detail == "all 3 blocks are in the cup of their colour"


def test_sorted_counts_a_block_resting_on_the_rim():
    # The fake world's cups are solid: a dropped block rests on top, centred in the opening.
    blocks = [block_in(c.color, c, z=0.08) for c in CUPS]
    assert check("sorted_by_color", ["block", "cup"], *blocks, *CUPS).ok


def test_sorted_names_a_block_in_the_wrong_cup():
    red_cup, green_cup, blue_cup = CUPS
    blocks = [block_in("red", green_cup), block_in("green", red_cup), block_in("blue", blue_cup)]
    r = check("sorted_by_color", [], *blocks, *CUPS)
    assert not r.ok
    assert r.detail.startswith("2 of 3 blocks not sorted by colour")
    assert "red block is in green cup, not red cup" in r.detail
    assert "green block is in red cup, not green cup" in r.detail
    assert "blue block" not in r.detail


def test_sorted_gives_distance_for_a_block_left_on_the_table():
    blocks = [block_in("red", CUPS[0]), block_in("green", CUPS[1]),
              obj("blue", "block", 0.15, -0.08)]
    r = check("sorted_by_color", [], *blocks, *CUPS)
    assert not r.ok
    assert "blue block is 10.2 cm from blue cup centre (must be < 3.2 cm)" in r.detail


def test_sorted_reports_missing_cup_and_empty_scene():
    r = check("sorted_by_color", [], block_in("blue", CUPS[2]), *CUPS[:2])
    assert not r.ok and "no blue cup for blue block" in r.detail
    r = check("sorted_by_color", [], *CUPS)
    assert not r.ok and "no blocks to check (saw: red cup, green cup, blue cup)" in r.detail
    r = check("sorted_by_color", [], RED_B)
    assert not r.ok and "no cups to sort blocks into" in r.detail


def test_sorted_accepts_dict_args_and_other_kinds():
    balls = [obj(c.color, "ball", c.x, c.y, z=0.02) for c in CUPS]
    r = evaluate({"op": "sorted_by_color", "args": {"kind": "ball", "into": "cup"}}, scene(*balls, *CUPS))
    assert r.ok and "all 3 balls" in r.detail


# --- left_of -----------------------------------------------------------------------------


def test_left_of_uses_plus_y_as_left():
    a, b = obj("red", "block", 0.2, 0.08), obj("green", "block", 0.2, 0.03)
    r = check("left_of", ["red block", "green block"], a, b)
    assert r.ok and "red block is 5.0 cm left of green block" in r.detail
    r = check("left_of", ["green block", "red block"], a, b)
    assert not r.ok
    assert "green block is 5.0 cm right of red block (y=0.030 vs y=0.080" in r.detail
    assert "it must be left" in r.detail


def test_left_of_margin():
    a, b = obj("red", "block", 0.2, 0.02), obj("green", "block", 0.2, 0.0)
    assert check("left_of", ["red block", "green block"], a, b).ok
    r = check("left_of", ["red block", "green block", 0.05], a, b)
    assert not r.ok and "it must be more than 5.0 cm left" in r.detail


def test_left_of_level_and_missing():
    a, b = obj("red", "block", 0.2, 0.0), obj("green", "block", 0.25, 0.0)
    r = check("left_of", ["red block", "green block"], a, b)
    assert not r.ok and "level with green block" in r.detail
    r = check("left_of", ["red block", "blue cup"], a, b)
    assert not r.ok and "cannot see 'blue cup'" in r.detail


# --- upright -----------------------------------------------------------------------------


def test_upright_cup_passes_and_tipped_cup_fails():
    assert check("upright", ["red cup"], cup("red", 0.25, 0.1)).ok
    tipped = obj("red", "cup", 0.25, 0.1, height=0.065, width=0.08)
    r = check("upright", ["red cup"], tipped)
    assert not r.ok
    assert "red cup looks tipped over: height 6.5 cm is less than its width 8.0 cm" in r.detail


def test_upright_with_expected_height():
    tipped = obj("red", "cup", 0.25, 0.1, height=0.065, width=0.08)
    r = check("upright", ["red cup", 0.08], tipped)
    assert not r.ok and "height 6.5 cm, upright it is 8.0 cm (tolerance 1.0 cm)" in r.detail
    assert check("upright", ["red cup", 0.08], cup("red", 0.25, 0.1)).ok


def test_upright_cube_and_hidden():
    assert check("upright", ["red block"], RED_B).ok
    hidden = obj("red", "cup", 0.25, 0.1, height=0.08, width=0.065, visible=False)
    r = check("upright", ["red cup"], hidden)
    assert not r.ok and "red cup is hidden" in r.detail


# --- composition and bad input -----------------------------------------------------------


def test_new_ops_compose_with_all_and_not():
    a, b = obj("red", "block", 0.2, 0.08), obj("green", "block", 0.2, 0.03)
    pred = {"op": "all", "args": [{"op": "left_of", "args": ["red block", "green block"]},
                                  {"op": "not", "args": [{"op": "upright", "args": ["red block", 0.06]}]}]}
    assert evaluate(pred, scene(a, b)).ok


def test_bad_args_give_a_readable_failure_not_an_exception():
    r = check("left_of", ["red block"], RED_B)
    assert not r.ok and r.detail.startswith("bad args for 'left_of'")
    r = check("upright", [], RED_B)
    assert not r.ok and "bad args for 'upright'" in r.detail


def test_unknown_op_lists_the_known_ones():
    r = check("stacked", [], RED_B)
    assert not r.ok and "unknown predicate op 'stacked'" in r.detail
    assert "sorted_by_color" in r.detail and "tower" in r.detail


# --- end to end on the fake world --------------------------------------------------------

LIB = REPO_ROOT / "skills/library"
SOURCES = {p.stem: p.read_text() for p in LIB.glob("*.py")}
SOURCES["stack"] = (LIB / "seeds/stack.py").read_text()


def run(skill, params, objects, success):
    w = FakeWorld.from_scene(Scene(tuple(objects), 0.0, "fake"), cfg)
    return run_skill(SOURCES[skill], Env("fake", w, w, w), params, cfg, NullBus(),
                     skill=skill, library=SOURCES, success=success)


FLOOR = (obj("red", "block", 0.15, 0.08), obj("green", "block", 0.15, 0.0), obj("blue", "block", 0.15, -0.08))


def test_tower_skill_satisfies_tower_predicate_on_fake_world():
    r = run("tower", {"objs": TOWER}, FLOOR, {"op": "tower", "args": TOWER})
    assert r.ok, r.reason()
    assert "tower of 3 stands" in r.verify.detail


def test_sort_skill_satisfies_sorted_by_color_on_fake_world():
    r = run("sort_by_color", {}, FLOOR + CUPS, {"op": "sorted_by_color", "args": ["block", "cup"]})
    assert r.ok, r.reason()
    assert r.verify.detail == "all 3 blocks are in the cup of their colour"


def test_untouched_table_fails_sorted_by_color_with_distances():
    r = run("tower", {"objs": TOWER[:2]}, FLOOR + CUPS, {"op": "sorted_by_color", "args": []})
    assert not r.ok
    assert "not sorted by colour" in r.reason() and "cm from" in r.reason()
