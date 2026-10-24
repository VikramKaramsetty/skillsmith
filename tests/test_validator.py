import pytest

from skillsmith.config import REPO_ROOT
from skillsmith.demo import demo_sources
from skillsmith.skills.validator import validate

HEADER = 'SKILL = {"name": "t", "params": {}, "uses": []}\n\n'


def skill(body: str) -> str:
    return HEADER + "def run(robot, scene, skills):\n" + "".join(f"    {line}\n" for line in body.splitlines())


def messages(source: str) -> str:
    return " | ".join(i.message for i in validate(source))


def test_seed_and_demo_skills_pass():
    for src in [(REPO_ROOT / "skills/library/pick_place.py").read_text(), *demo_sources()]:
        assert validate(src) == []


def test_good_skill_with_helper_passes():
    src = HEADER + (
        "def lift(robot, z):\n    robot.move_to(0.2, 0.0, z)\n\n"
        "def run(robot, scene, skills):\n"
        "    for o in scene.objects:\n        robot.note(o.label)\n"
        "    for i in range(3):\n        lift(robot, 0.1 + i * 0.01)\n"
        "    skills.call('pick_place', obj='blue cup', x=0.2, y=0.0)\n"
    )
    assert validate(src) == []


@pytest.mark.parametrize("body,expected", [
    ("import os", "imports are not allowed"),
    ("while True:\n    robot.home()", "while loops"),
    ("try:\n    robot.home()\nexcept Exception:\n    pass", "try/except"),
    ("robot.set_torque(False)", "not in the API"),
    ("scene.objects.__class__", "private"),
    ("r = robot\nr.home()", "use 'robot' only"),
    ("getattr(robot, 'home')()", "'getattr' is not allowed"),
    ("for i in range(1000):\n    robot.home()", "range bound 1000"),
    ("robot = None", "do not reassign"),
    ("f = lambda: 1", "lambda"),
    ("open('/etc/passwd')", "'open' is not allowed"),
])
def test_rejections(body, expected):
    assert expected in messages(skill(body))


def test_missing_run_and_bad_signature():
    assert "missing `def run" in messages(HEADER + "def go(robot):\n    pass\n")
    assert "must start with parameters" in messages(HEADER + "def run(scene, robot, skills):\n    pass\n")


def test_top_level_code_rejected():
    assert "top level" in messages(skill("robot.home()") + "print('hi')\n")


def test_syntax_error_reported_with_line():
    issues = validate("def run(robot, scene, skills):\n    robot.move_to(0.1,\n")
    assert issues and "syntax error" in issues[0].message
