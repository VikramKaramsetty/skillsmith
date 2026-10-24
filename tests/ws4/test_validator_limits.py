"""WS4.9: repetition and bigint growth driven by variables, and aliases of robot/scene data.

The sim gate runs in-process, so one C-level step (a 10**9-element list, a squared bigint, a
factorial) hangs or kills the orchestrator where no timeout can reach. Each rejected case here
passed validate() before WS4.9 (issue #158). The legal cases pin down that ordinary skill code,
and every skill in skills/library/, still passes.
"""

import time

import pytest

from skillsmith.config import REPO_ROOT
from skillsmith.demo import demo_sources
from skillsmith.skills.runtime import SkillValidationError, compile_skill
from skillsmith.skills.validator import validate

HEADER = 'SKILL = {"name": "t", "params": {}, "uses": []}\n\n'


def skill(body: str, prelude: str = "", params: str = "") -> str:
    lines = "".join(f"    {line}\n" for line in body.splitlines())
    return HEADER + prelude + f"def run(robot, scene, skills{params}):\n" + lines


def messages(source: str) -> str:
    return " | ".join(i.message for i in validate(source))


LIBRARY = sorted((REPO_ROOT / "skills" / "library").rglob("*.py"))


def test_library_is_not_empty():
    assert len(LIBRARY) >= 6 and any(p.parent.name == "seeds" for p in LIBRARY)


@pytest.mark.parametrize("path", LIBRARY, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_library_skill_still_validates(path):
    assert validate(path.read_text()) == []


def test_demo_skills_still_validate():
    for src in demo_sources():
        assert validate(src) == []


# The exact snippets from issue #158, plus the alias mutation.
ISSUE_CASES = [
    ("k = 10 ** 9\nbuf = [0] * k", "repeat a sequence only a constant number of times"),
    ("k = 10 ** 9\ns = 'x' * k", "repeat a sequence only a constant number of times"),
    ("n = 3\nfor i in range(40):\n    n = n * n", "'n' grows from its own previous value"),
    ("n = math.factorial(10 ** 9)", "math.factorial() needs a constant argument <= 1000"),
    ("objs = scene.objects\nobjs.clear()", "'objs' is scene data (bound on line 4) and read-only"),
]


@pytest.mark.parametrize("body,expected", ISSUE_CASES)
def test_issue_158_snippets_are_rejected(body, expected):
    assert expected in messages(skill(body))


@pytest.mark.parametrize("body,expected", ISSUE_CASES)
def test_issue_158_snippets_never_reach_exec(body, expected):
    with pytest.raises(SkillValidationError, match="failed static checks"):
        compile_skill(skill(body), "t")


@pytest.mark.parametrize("body,prelude,params,expected", [
    # sequence repetition by a variable, or of a non-literal sequence
    ("k = 10 ** 9\nbuf = k * [0]", "", "", "repeat a sequence only a constant number of times"),
    ("flags = [False] * len(objs)", "", ", objs", "use a comprehension, e.g. [0 for _ in objs]"),
    ("s = 'ab'\nt = s * 1000\nu = t * 1000", "", "", "repeat only a literal list or string"),
    ("s = 'x'\ns *= 1000", "", "", "repeat only a literal list or string"),
    ("row = list(objs) * 3", "", ", objs", "repeat only a literal list or string"),
    ("s = f'{objs}' * 2", "", ", objs", "repeat only a literal list or string"),
    # values that grow from themselves inside a loop
    ("n = 3\nfor i in range(40):\n    n *= n", "", "", "'n' grows from its own previous value"),
    ("n = 3\nfor i in range(40):\n    n = n ** 2", "", "", "'n' grows from its own previous value"),
    ("s = [0]\nfor i in range(40):\n    s = s * 2", "", "", "'s' grows from its own previous value"),
    ("s = 'x'\nfor i in range(40):\n    s = s + s", "", "", "'s' grows from its own previous value"),
    ("s = 'x'\nfor i in range(40):\n    s += s", "", "", "'s' grows from its own previous value"),
    ("a = 3\nb = 3\nfor i in range(40):\n    a = b * b\n    b = a", "", "", "'a' grows from its own previous"),
    ("n = 3\nfor i in range(5):\n    for j in range(8):\n        n = n * n", "", "", "'n' grows"),
    ("n = 3\nxs = [(n := n * n) for _ in range(40)]", "", "", "'n' grows from its own previous value"),
    ("n = 3\nfor i in range(40):\n    n = sq(n)", "def sq(n):\n    return n * n\n\n", "", "'n' grows"),
    # calls whose argument sets the size of the result
    ("k = len(objs)\nn = math.comb(k, 2)", "", ", objs", "math.comb() needs a constant argument"),
    ("n = math.perm(10 ** 6, 3)", "", "", "math.perm() needs a constant argument <= 1000"),
    ("f = math.factorial\nn = f(10 ** 9)", "", "", "call .factorial(...) directly"),
    ("k = 10 ** 9\nrobot.note('x'.ljust(k))", "", "", ".ljust() needs a constant argument"),
    ("robot.note('7'.zfill(width=10 ** 9))", "", "", ".zfill() needs a constant argument"),
    ("b = (1).to_bytes(10 ** 9, 'big')", "", "", ".to_bytes() needs a constant argument"),
    ("pad = 'x'.center\nrobot.note(pad(10 ** 9))", "", "", "call .center(...) directly"),
    ("k = 10 ** 9\nrobot.note(f'{1:>{k}}')", "", "", "f-string widths and precisions must be constants"),
    ("robot.note(f'{1.0:.1000000000f}')", "", "", "f-string width or precision over 1000"),
    ("k = 10 ** 9\nrobot.note('%*d' % (k, 1))", "", "", "%-formatting is not allowed"),
    # a computed range consumed by one C-level call
    ("k = 10 ** 9\nxs = list(range(k))", "", "", "range() with a computed bound may only drive a for loop"),
    ("k = 10 ** 9\nt = sum(range(k))", "", "", "range() with a computed bound may only drive a for loop"),
    # constants the validator must refuse to compute (it would hang, and so would the import)
    ("robot.home()", "A = 10 ** 10\nB = A * A\nC = B * B\nD = C * C\n\n", "", "is too large (over 2**256)"),
    ("for i in range(N * N):\n    pass", "N = 10 ** 10 * 10 ** 10 * 10 ** 10 * 10 ** 10 * 10 ** 10\n\n", "", "too large"),
    # aliases of robot/scene data are read-only too
    ("objs = scene.objects\nobjs[0] = None", "", "", "'objs' is scene data"),
    ("objs = scene.objects\nobjs += [1]", "", "", "'+=' on a list would change it"),
    ("objs = scene.objects or []\nobjs.append(1)", "", "", "'objs' is scene data"),
    ("a = b = scene.objects\nb.sort()", "", "", "'b' is scene data"),
    ("if (objs := scene.objects):\n    objs.clear()", "", "", "'objs' is scene data"),
    ("objs = scene.objects\nmine = objs\nmine.pop()", "", "", "'mine' is scene data"),
    ("for o in scene.objects:\n    o.tags.append('x')", "", "", "'o' is scene data"),
    ("wipe(scene.objects)", "def wipe(lst):\n    lst.clear()\n\n", "", "'lst' is scene data"),
    ("everything(scene).clear()", "def everything(scene):\n    return scene.objects\n\n", "", "is scene data"),
    ("p = robot.position()\np.reverse()", "", "", "'p' is robot data"),
])
def test_variable_driven_rejections(body, prelude, params, expected):
    assert expected in messages(skill(body, prelude, params))


@pytest.mark.parametrize("body,prelude,params", [
    # copies of scene data are the skill's own to change
    ("objs = list(scene.objects)\nobjs.append(1)\nobjs.sort(key=len)", "", ""),
    ("objs = scene.objects[:]\nobjs.reverse()", "", ""),
    ("picked = []\nfor o in scene.objects:\n    picked.append(o.label)", "", ""),
    ("labels = [o.label for o in scene.objects]\nlabels.sort()", "", ""),
    ("cup = scene.require('cup')\nz = cup.z\nz += 0.01\nrobot.move_to(cup.x, cup.y, z)", "", ""),
    # ordinary numeric code in loops: increments, float scaling, products of fresh values
    ("x = 0.0\nfor i in range(10):\n    x = x + (0.2 - x) * gain", "", ", gain=0.5"),
    ("step = 0.04\nfor i in range(10):\n    step = step * 0.5\n    step *= SHRINK", "SHRINK = 0.9\n\n", ""),
    ("shrink = 0.5\nstep = 0.04\nfor i in range(10):\n    step *= shrink", "", ""),
    ("total = 0.0\nfor o in scene.objects:\n    total = total + o.width * o.height", "", ""),
    ("for o in scene.objects:\n    w = o.width\n    h = o.height\n    area = w * h", "", ""),
    ("for o in scene.objects:\n    d = (o.x * o.x + o.y * o.y) ** 0.5\n    robot.note(f'{d:.3f}')", "", ""),
    ("n = 1\nfor i in range(1, 6):\n    n = n * i", "", ""),
    # computed ranges that drive a loop, constant repetition of literals, small factorials
    ("for i in range(len(objs)):\n    robot.note(objs[i])", "", ", objs"),
    ("xs = [i * 0.01 for i in range(len(objs))]", "", ", objs"),
    ("flags = [False for _ in objs]\nbar = '-' * 20\nrow = [0, 0] * 3", "", ", objs"),
    ("n = math.factorial(5) + math.comb(6, 2)\nrobot.note(f'{n:>8}|{0.5:.3f}')", "", ""),
    ("robot.note('7'.zfill(3) + 'a'.ljust(10))", "", ""),
])
def test_legal_code_still_passes(body, prelude, params):
    assert validate(skill(body, prelude, params)) == []


def test_squaring_constants_does_not_hang_the_validator():
    lines = ["X0 = 10 ** 10"] + [f"X{i} = X{i - 1} * X{i - 1}" for i in range(1, 60)]
    src = skill("robot.home()", "\n".join(lines) + "\n\n")
    t0 = time.monotonic()
    issues = validate(src)
    assert time.monotonic() - t0 < 2.0
    assert sum("too large" in i.message for i in issues) == 1  # reported once, where it first overflows


def test_rejections_say_what_to_write_instead():
    hints = ("use", "copy", "compute", "write", "call", "repeat only", "e.g.")
    for body, _ in ISSUE_CASES:
        for issue in validate(skill(body)):
            assert any(h in issue.message for h in hints), issue.message
