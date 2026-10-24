"""WS4.1: adversarial skills that the validator must reject, and legal code it must keep passing.

Each case is something a careless or hostile skill could do to escape the sandbox, alias a
handle past the API check, mutate the scene, or hang the process where a timeout cannot reach.
"""

import pytest

from skillsmith.skills import runtime
from skillsmith.skills.runtime import SAFE_BUILTINS, SkillValidationError
from skillsmith.skills.validator import ALLOWED_BUILTINS, validate

HEADER = 'SKILL = {"name": "t", "params": {}, "uses": []}\n\n'


def skill(body: str, prelude: str = "") -> str:
    return HEADER + prelude + "def run(robot, scene, skills):\n" + "".join(f"    {line}\n" for line in body.splitlines())


def messages(source: str) -> str:
    return " | ".join(i.message for i in validate(source))


FRAME_ESCAPE = skill(
    "g = (g.gi_frame.f_back.f_back.f_globals for _ in [0])\n"
    "return list(g)[0]['builtins'].open"
)


def test_generator_frame_escape_is_rejected_before_it_runs():
    # Before WS4.1 this passed the validator and returned the real `open` from the runtime's globals.
    assert "runtime introspection" in messages(FRAME_ESCAPE)
    with pytest.raises(SkillValidationError):
        runtime._invoke(FRAME_ESCAPE, "t", None, None, None, {})


@pytest.mark.parametrize("body,prelude,expected", [
    # sandbox escapes without an underscore
    ("t = (x for x in [0]).gi_code", "", "runtime introspection"),
    ("f = (x for x in [0]).gi_frame\nrobot.note(f.f_locals)", "", "runtime introspection"),
    ("robot.note('{0._env}'.format(robot))", "", "use an f-string instead"),
    ("robot.note('{r._env}'.format_map({'r': 1}))", "", "use an f-string instead"),
    ("base = int.mro()[1]", "", "class introspection"),
    # names the runtime does not provide (would only fail later, as a confusing NameError)
    ("for x in iter(int, 1):\n    robot.home()", "", "name 'iter' is not available"),
    ("print('lowering')", "", "use robot.note(text)"),
    # aliasing a handle through a helper so robot.<anything> escapes the API check
    ("h(robot)", "def h(r):\n    r.set_torque(False)\n\n", "pass 'robot' only to a parameter named 'robot'"),
    ("h(r=robot)", "def h(r):\n    r.set_torque(False)\n\n", "pass 'robot' only to a parameter named 'robot'"),
    ("h(robot)", "def h(skills):\n    skills.call('x')\n\n", "pass 'robot' only to a parameter named 'robot'"),
    ("objs = sorted(scene)", "", "pass 'scene' only to a parameter named 'scene'"),
    ("def h(r=robot):\n    r.set_torque(False)\nh()", "", "use 'robot' only"),
    ("match robot:\n    case r:\n        r.set_torque(False)", "", "use 'robot' only"),
    ("for robot in [1]:\n    pass", "", "do not reassign 'robot'"),
    ("if (scene := 1):\n    pass", "", "do not reassign 'scene'"),
    # writing to robot or scene state instead of reading it
    ("robot.move_to = robot.home", "", "do not assign to attributes"),
    ("cup = scene.require('cup')\ncup.z = 0.3", "", "do not assign to attributes"),
    ("scene.objects.clear()", "", "scene data is read-only"),
    ("scene.objects[0] = None", "", "scene data is read-only"),
    # loops that only look bounded
    ("for i in range(10 ** 6):\n    robot.home()", "", "range bound 1000000 > 50"),
    ("for i in range(N):\n    robot.home()", "N = 1000\n\n", "range bound 1000 > 50"),
    ("for i in range(2, -(-500)):\n    robot.home()", "", "range bound 500 > 50"),
    ("run(robot, scene, skills)", "", "recursion is not allowed (run -> run)"),
    ("a(robot)", "def a(robot):\n    b(robot)\n\ndef b(robot):\n    a(robot)\n\n", "recursion is not allowed (a -> b -> a)"),
    # memory and CPU bombs a wall-clock timeout cannot interrupt (one C-level operation)
    ("buf = [0] * 10 ** 9", "", "sequence repeated 1000000000 times"),
    ("s = 'x' * N", "N = 10 ** 8\n\n", "sequence repeated 100000000 times"),
    ("n = 2 ** 10 ** 10", "", "exponent must be a number"),
    ("k = 3\nn = 2 ** k", "", "exponent must be a number"),
    ("n = 1 << 10 ** 9", "", "bit shifts are not allowed"),
    ("n = 2\nn **= 99", "", "exponent must be a number"),
    # module-level code runs at import, before any timeout or safety check
    ("robot.home()", "BUF = [0] * 10\nX = len(BUF)\n\n", "top-level assignments must be constants"),
    ("robot.home()", "@robot.note\ndef h(robot):\n    pass\n\n", "decorators are not allowed"),
])
def test_adversarial_rejections(body, prelude, expected):
    assert expected in messages(skill(body, prelude))


def test_bomb_in_top_level_constant_does_not_hang_the_validator():
    # Folding must refuse to compute the power; _check_arith rejects it instead.
    assert "exponent must be a number" in messages(skill("robot.home()", "N = 10 ** 10 ** 10\n\n"))


def test_every_rejection_says_what_to_do_instead():
    hints = ("use", "instead", "only", "copy", "loop", "limit", "allowed", "build", "call", "compute", "write")
    for body, prelude in [("import os", ""), ("getattr(robot, 'home')()", ""), ("x = robot._env", ""),
                          ("print(1)", ""), ("h(robot)", "def h(r):\n    pass\n\n"),
                          ("scene.objects.pop()", ""), ("n = 2 ** 99", ""), ("robot = 1", "")]:
        for issue in validate(skill(body, prelude)):
            assert any(h in issue.message for h in hints), issue.message


def test_legal_skill_with_helpers_comprehensions_and_constants_passes():
    src = HEADER + (
        "HOVER = 0.05 + 0.03\nN = 3\nCOLORS = ['red', 'blue']\nLIMIT = math.pi / 2\n\n"
        "def lift(robot, z):\n    robot.move_to(0.2, 0.0, z)\n\n"
        "def width(o):\n    return o.width\n\n"
        "def run(robot, scene, skills, objs=None):\n"
        "    picked = []\n"
        "    for o in sorted(scene.objects, key=width):\n"
        "        if any(c == o.color for c in COLORS):\n"
        "            picked.append(o.label)\n"
        "    d = (picked and 0.1 ** 2 + 0.2 ** 2) ** 0.5\n"
        "    for i in range(N * 2):\n"
        "        lift(robot=robot, z=HOVER + i * 0.01)\n"
        "    def down(robot):\n        robot.move_to(0.2, 0.0, 0.02)\n"
        "    down(robot)\n"
        "    robot.note(f'picked {len(picked)} at {d:.2f}: {picked}')\n"
        "    skills.call('pick_place', obj='blue cup', x=0.2, y=0.0)\n"
    )
    assert validate(src) == []


def test_runtime_builtins_match_the_validator_allow_list():
    assert set(SAFE_BUILTINS) == set(ALLOWED_BUILTINS) | {"SkillError"}
