"""Contracts added in plan v2: align_over, the live-world bump stop, classification in reports."""

from skillsmith.config import load_config
from skillsmith.events import NullBus
from skillsmith.hal.base import Env
from skillsmith.hal.fake import FakeWorld
from skillsmith.repair.report import attempt_report
from skillsmith.skills.runtime import run_skill
from skillsmith.tasks import parse_task
from skillsmith.types import Classification, PhaseCheck

cfg = load_config()
HEADER = 'SKILL = {"name": "t", "params": {}, "uses": []}\n\n'


def fake_env():
    w = FakeWorld.demo(cfg)
    return Env("fake", w, w, w)


def test_align_over_hovers_above_latest_position():
    src = HEADER + "def run(robot, scene, skills):\n    err = robot.align_over('green block', hover=0.05)\n"
    env = fake_env()
    r = run_skill(src, env, {}, cfg, NullBus(), skill="t")
    assert r.ok, r.reason()
    x, y, z = env.kin.fk(env.arm.joints())
    assert (round(x, 3), round(y, 3), round(z, 3)) == (0.15, 0.0, 0.08)  # block top 0.03 + 0.05


class BumpingPerception:
    """Wraps the fake world; reports one unexpected motion after the first call."""

    def __init__(self, world):
        self.world, self.calls = world, 0

    def observe(self):
        return self.world.observe()

    def snapshot(self, path):
        return None

    def unexpected_motion(self):
        self.calls += 1
        return "red cup moved 3.2 cm toward +y while not held" if self.calls == 3 else None


def test_world_change_stops_before_next_motion():
    w = FakeWorld.demo(cfg)
    env = Env("fake", w, w, BumpingPerception(w))
    src = HEADER + ("def run(robot, scene, skills):\n"
                    "    robot.move_to(0.2, 0.0, 0.15)\n    robot.move_to(0.2, 0.05, 0.15)\n"
                    "    robot.move_to(0.2, 0.10, 0.15)\n")
    bus = NullBus()
    r = run_skill(src, env, {}, cfg, bus, skill="t")
    assert not r.ok and r.error.startswith("WorldChanged") and "red cup moved" in r.error
    assert "world.bumped" in bus.types()
    assert sum(1 for c in r.trace if c.ok and c.name == "move_to") == 2  # third move never ran


def test_report_leads_with_classification():
    src = HEADER + "def run(robot, scene, skills):\n    robot.home()\n"
    r = run_skill(src, fake_env(), {}, cfg, NullBus(), skill="t",
                  success={"op": "on_top", "args": ["blue cup", "red cup"]})
    r.classification = Classification("NO_PICK", "skill", "approach", 0,
                                      "blue cup never left the table (moved 0.0 cm)", hint="grasp", escalate=True)
    r.phases = [PhaseCheck("approach", False, "never descended to the blue cup", 0, 0)]
    md = attempt_report(parse_task("stack the blue cup on the red cup"), r, [])
    assert md.index("`NO_PICK`") < md.index("**Task:**")
    assert "Change the approach" in md and "## Phases" in md
    assert r.reason().startswith("NO_PICK:")
