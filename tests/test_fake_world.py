from skillsmith.config import REPO_ROOT, load_config
from skillsmith.demo import demo_sources
from skillsmith.events import NullBus
from skillsmith.hal.base import Env
from skillsmith.hal.fake import FakeWorld
from skillsmith.skills.runtime import run_skill

cfg = load_config()
PICK_PLACE = (REPO_ROOT / "skills/library/pick_place.py").read_text()
STACK = {"op": "on_top", "args": ["blue cup", "red cup"]}


def env(rim_slip=False):
    w = FakeWorld.demo(cfg, rim_slip=rim_slip)
    return Env("fake", w, w, w)


def test_pick_place_moves_block():
    r = run_skill(PICK_PLACE, env(), {"obj": "green block", "x": 0.15, "y": 0.12}, cfg, NullBus(),
                  skill="pick_place", success={"op": "near", "args": ["green block", 0.15, 0.12]})
    assert r.ok, r.reason()
    assert r.safety.ok and len(r.trace) > 5


def test_lowering_held_cup_into_other_cup_collides():
    bad = PICK_PLACE.replace("place_z + grip_offset + 0.005", "place_z + 0.01")
    r = run_skill(bad, env(), {"obj": "blue cup", "x": 0.22, "y": 0.08}, cfg, NullBus(),
                  skill="pick_place", success=STACK)
    assert not r.ok
    assert r.error.startswith("CollisionError") and "pick_place.py line" in r.error
    assert any(i.code == "COLLISION" for i in r.safety.issues)


def test_rim_grip_slips_only_in_real_mode():
    v1, _ = demo_sources()
    params = {"top": "blue cup", "bottom": "red cup"}
    sim = run_skill(v1, env(rim_slip=False), params, cfg, NullBus(), skill="stack", success=STACK)
    real = run_skill(v1, env(rim_slip=True), params, cfg, NullBus(), skill="stack", success=STACK)
    assert sim.ok, sim.reason()
    assert not real.ok and "lost blue cup" in real.error


def test_unsafe_target_never_moves():
    src = 'SKILL = {}\n\ndef run(robot, scene, skills):\n    robot.move_to(0.6, 0.0, 0.1)\n'
    e = env()
    before = e.arm.joints()
    r = run_skill(src, e, {}, cfg, NullBus(), skill="t")
    assert not r.ok and "OUT_OF_BOX" in r.error
    assert e.arm.joints() == before


def test_released_object_lands_on_support_not_on_itself():
    """Regression: _drop used to count the dropped object as its own support (+8 cm float)."""
    _, v2 = demo_sources()
    e = env()
    r = run_skill(v2, e, {"top": "blue cup", "bottom": "red cup"}, cfg, NullBus(), skill="stack",
                  library={"pick_place": PICK_PLACE}, success=STACK)
    blue, red = r.scene_after.find("blue cup"), r.scene_after.find("red cup")
    assert r.ok and abs(blue.z - red.top) < 1e-6
