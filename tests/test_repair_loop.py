import pytest

from skillsmith.config import load_config
from skillsmith.demo import demo_sources, temp_library
from skillsmith.events import NullBus
from skillsmith.hal.base import Env, make_env
from skillsmith.hal.fake import FakeWorld
from skillsmith.repair.approvers import AutoApprover
from skillsmith.repair.loop import RepairLoop
from skillsmith.repair.scripted import ScriptedAuthor
from skillsmith.skills.library import SkillLibrary
from skillsmith.tasks import parse_task

cfg = load_config()
TASK = "stack the blue cup on the red cup"


def make_loop(author, approver=None, rim_slip=True):
    w = FakeWorld.demo(cfg, rim_slip=rim_slip)
    bus = NullBus()
    library = SkillLibrary(temp_library())
    loop = RepairLoop(cfg, Env("fake", w, w, w), lambda s: make_env("fake", cfg, s), library,
                      author, approver or AutoApprover("fake"), bus)
    return loop, bus, library


def test_demo_story_fail_on_real_then_repair():
    v1, v2 = demo_sources()
    author = ScriptedAuthor([v1], [v2])
    loop, bus, library = make_loop(author)
    result = loop.run(parse_task(TASK))

    assert result.ok and result.rounds == 1
    assert library.has("stack") and "pick_place" in library.load("stack")
    assert "lost blue cup" in author.reports[0]  # Droid was told why
    types = bus.types()
    for expected in ["plan.selected", "gate.result", "approval.requested", "attempt.finished",
                     "repair.started", "skill.diff", "skill.saved", "run.finished"]:
        assert expected in types
    finished = [e["data"]["ok"] for e in bus.events if e["type"] == "attempt.finished"]
    assert finished == [True, False, True, True]  # sim v1, real v1, sim v2, real v2


def test_static_gate_feeds_back_before_anything_moves():
    _, v2 = demo_sources()
    bad = "import os\n" + v2
    author = ScriptedAuthor([bad], [v2])
    loop, bus, _ = make_loop(author, rim_slip=False)
    result = loop.run(parse_task(TASK))
    assert result.ok and result.rounds == 1
    assert "imports are not allowed" in author.reports[0]
    gates = [(e["data"]["gate"], e["data"]["ok"]) for e in bus.events if e["type"] == "gate.result"]
    assert gates[0] == ("static", False)


class RejectOnce:
    def __init__(self):
        self.calls = 0

    def request(self, run_id, summary):
        self.calls += 1
        return (False, "too fast near the cup") if self.calls == 1 else (True, "")


def test_human_rejection_becomes_report():
    _, v2 = demo_sources()
    author = ScriptedAuthor([v2], [v2])
    loop, _, _ = make_loop(author, approver=RejectOnce(), rim_slip=False)
    assert loop.run(parse_task(TASK)).ok
    assert "too fast near the cup" in author.reports[0]


def test_gives_up_after_max_rounds():
    v1, _ = demo_sources()
    author = ScriptedAuthor([v1], [v1] * cfg.repair.max_rounds)
    loop, bus, library = make_loop(author)
    result = loop.run(parse_task(TASK))
    assert not result.ok and result.rounds == cfg.repair.max_rounds
    assert not library.has("stack")


def test_auto_approver_refuses_real_arm():
    with pytest.raises(ValueError):
        AutoApprover("real")
