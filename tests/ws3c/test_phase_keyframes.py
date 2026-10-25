"""Real-run evidence: frames captured after each motion call become one annotated picture per phase."""

from pathlib import Path

import pytest

from skillsmith.config import load_config
from skillsmith.events import EventBus
from skillsmith.hal.base import make_env
from skillsmith.skills.runtime import run_skill

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

SKILL = '''
SKILL = {"name": "pick_place", "params": {}}

def run(robot, scene, skills):
    o = scene.require("blue cup")
    robot.release()
    robot.move_to(o.x, o.y, 0.15)
    robot.move_to(o.x, o.y, o.z + 0.04)
    robot.grasp()
    robot.move_to(o.x, o.y, 0.15)
'''


class _Cam:
    def project(self, x, y, z):
        return 320.0 + x * 100, 240.0 + y * 100


def test_each_phase_gets_an_annotated_overhead_plus_wrist_frame(tmp_path):
    cfg = load_config()
    env = make_env("fake", cfg)
    env.perception.camera = _Cam()
    env.perception.keyframe_images = lambda: (np.full((480, 640, 3), 128, np.uint8), np.full((480, 640, 3), 60, np.uint8))
    bus = EventBus("t1", runs_dir=tmp_path)
    res = run_skill(SKILL, env, {}, cfg, bus, skill="pick_place")
    assert res.phases and res.keyframes
    framed = [p for p in res.phases if p.frame_path]
    assert framed and all(Path(p.frame_path).is_file() for p in framed)
    assert set(res.keyframes) == {p.phase for p in framed}
