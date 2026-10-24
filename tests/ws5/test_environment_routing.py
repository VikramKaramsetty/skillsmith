"""WS5.5: environment-bucket failures go to a human, never to Droid; Droid's change_type
drives escalation; the template forbids magic offsets.

analysis.analyse_attempt is still a WS4 stub, so these tests patch in a fixed classifier.
"""

from pathlib import Path

import pytest

from skillsmith.config import load_config
from skillsmith.demo import demo_sources, temp_library
from skillsmith.events import NullBus
from skillsmith.hal.base import Env, make_env
from skillsmith.hal.fake import FakeWorld
from skillsmith.repair.approvers import AutoApprover
from skillsmith.repair.droid_tools import CHANGE_TYPES, REPAIR_RESULT_SCHEMA
from skillsmith.repair.loop import RepairLoop
from skillsmith.repair.scripted import ScriptedAuthor
from skillsmith.skills.library import SkillLibrary
from skillsmith.tasks import parse_task
from skillsmith.types import FAILURE_CLASSES, Classification

cfg = load_config()
TASK = "stack the blue cup on the red cup"
ESCALATE = "Change the approach"
NOT_FOUND = '''"""stack: put one object on top of another."""

SKILL = {"name": "stack", "params": {"top": "str", "bottom": "str"}, "uses": []}


def run(robot, scene, skills, top, bottom):
    raise SkillError("blue cup not found")
'''


def classify(monkeypatch, *classes):
    """Label failed attempts with `classes` in order; the last one repeats."""
    queue = list(classes)

    def fake(attempt, _cfg):
        if attempt.ok:
            return [], None
        cls = queue.pop(0) if len(queue) > 1 else queue[0]
        return [], Classification(cls, FAILURE_CLASSES[cls], "carry", 0, f"{cls} in the test")

    monkeypatch.setattr("skillsmith.analysis.analyse_attempt", fake)


class Human:
    """Approves code; answers environment requests with `fixed`. Records what it was asked."""

    def __init__(self, fixed=True):
        self.fixed = fixed
        self.environment = []

    def request(self, run_id, summary):
        if summary.get("kind") == "environment":
            self.environment.append(summary)
            return self.fixed, "" if self.fixed else "camera is broken"
        return True, ""


class ChangeTypeAuthor(ScriptedAuthor):
    """A ScriptedAuthor that reports a change_type like DroidSDKAuthor.last_result."""

    def __init__(self, drafts, repairs, change_type):
        super().__init__(drafts, repairs)
        self.last_result = {"change_type": change_type}


def make_loop(author, approver, rim_slip=True):
    w = FakeWorld.demo(cfg, rim_slip=rim_slip)
    bus = NullBus()
    loop = RepairLoop(cfg, Env("fake", w, w, w), lambda s: make_env("fake", cfg, s),
                      SkillLibrary(temp_library()), author, approver, bus)
    return loop, bus


def env_errors(bus):
    return [e["data"]["message"] for e in bus.events
            if e["type"] == "error" and e["data"]["where"] == "repair.environment"]


def test_environment_failure_goes_to_a_human_not_droid(monkeypatch):
    classify(monkeypatch, "CAMERA_MOVED")
    v1, v2 = demo_sources()
    author, human = ScriptedAuthor([v1], [v2]), Human(fixed=False)
    loop, bus = make_loop(author, human)
    result = loop.run(parse_task(TASK))

    assert not result.ok and result.rounds == 0
    assert author.reports == []  # Droid never saw it
    assert "repair.started" not in bus.types()
    (asked,) = human.environment
    assert asked["class"] == "CAMERA_MOVED" and "not a code problem" in asked["title"]
    requested = [e["data"]["summary"] for e in bus.events if e["type"] == "approval.requested"]
    assert requested[-1]["kind"] == "environment"  # visible on the dashboard
    assert env_errors(bus) and "Droid was not called" in env_errors(bus)[0]


def test_fixed_environment_retries_same_code_then_skill_failure_reaches_droid(monkeypatch):
    classify(monkeypatch, "OBJECT_OUT_OF_REACH", "SLIP_IN_CARRY")
    v1, v2 = demo_sources()
    author, human = ScriptedAuthor([v1], [v2]), Human(fixed=True)
    loop, bus = make_loop(author, human)
    result = loop.run(parse_task(TASK))

    assert result.ok and result.rounds == 1
    assert len(human.environment) == 1
    assert [a.ok for a in result.attempts] == [False, False, True]  # v1, v1 retried, v2
    assert len(author.reports) == 1 and "SLIP_IN_CARRY" in author.reports[0]
    assert "OBJECT_OUT_OF_REACH" not in author.reports[0].split("## ")[0]


def test_environment_failure_in_sim_gate_never_reaches_droid(monkeypatch):
    classify(monkeypatch, "OBJECT_NOT_FOUND")
    author = ScriptedAuthor([NOT_FOUND], [])
    loop, bus = make_loop(author, AutoApprover("fake"), rim_slip=False)
    result = loop.run(parse_task(TASK))

    assert not result.ok and author.reports == []
    kinds = [e["data"]["summary"].get("kind") for e in bus.events if e["type"] == "approval.requested"]
    assert kinds == ["environment"] * cfg.repair.max_rounds  # bounded, then stop
    assert len(env_errors(bus)) == 1


def test_unknown_bucket_still_goes_to_droid(monkeypatch):
    classify(monkeypatch, "UNKNOWN")
    v1, v2 = demo_sources()
    author = ScriptedAuthor([v1], [v2])
    loop, _ = make_loop(author, AutoApprover("fake"))
    assert loop.run(parse_task(TASK)).ok
    assert "low confidence" in author.reports[0]


@pytest.mark.parametrize("change_type, escalated", [("parameter", True), ("strategy", False)])
def test_same_class_after_change_type_escalates(monkeypatch, change_type, escalated):
    classify(monkeypatch, "SLIP_IN_CARRY")
    v1, v2 = demo_sources()
    author = ChangeTypeAuthor([v1], [v1, v2], change_type)  # first repair changes nothing useful
    loop, _ = make_loop(author, AutoApprover("fake"))
    assert loop.run(parse_task(TASK)).ok

    assert len(author.reports) == 2
    assert ESCALATE not in author.reports[0]
    assert (ESCALATE in author.reports[1]) is escalated


def test_result_schema_requires_change_type():
    prop = REPAIR_RESULT_SCHEMA["properties"]["change_type"]
    assert set(prop["enum"]) == set(CHANGE_TYPES) == {"parameter", "strategy"}
    assert "change_type" in REPAIR_RESULT_SCHEMA["required"]


def test_template_has_change_type_and_no_magic_offsets_rule():
    text = (Path(__file__).resolve().parents[2] / "droid_template" / "AGENTS.md").read_text()
    assert "`change_type`" in text
    assert "## No magic offsets" in text and "calibration" in text
