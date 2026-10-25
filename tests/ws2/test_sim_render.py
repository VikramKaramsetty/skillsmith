"""WS2.5: sim attempts render an mp4 and per-phase key frames from the overhead camera pose."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from skillsmith.config import Config, load_config
from skillsmith.events import EventBus
from skillsmith.hal.base import make_env
from skillsmith.hal.sim import CameraPose, SimRecorder, build_world, overhead_camera
from skillsmith.skills.runtime import run_skill
from skillsmith.types import PhaseCheck, Scene, SceneObject

mujoco = pytest.importorskip("mujoco")
pytest.importorskip("placo")
np = pytest.importorskip("numpy")
iio = pytest.importorskip("imageio.v3")

BASE = load_config()
MJCF = BASE.path(BASE.robot.mjcf)
pytestmark = pytest.mark.skipif(not MJCF.is_file(), reason="SO101 MJCF not vendored (WS2.1)")

BLUE = SceneObject("blue_block_0", "blue block", "blue", "block", 0.22, 0.10, 0.0, 0.03, 0.04)

PICK = '''
SKILL = {"name": "pick_test", "params": {"obj": "str"}}


def run(robot, scene, skills, obj):
    o = scene.require(obj)
    robot.release()
    robot.move_to(o.x, o.y, 0.10)
    robot.move_to(o.x, o.y, o.z + o.height * 0.5)
    robot.grasp()
    robot.move_to(o.x, o.y, 0.12)
'''


class _Bus(EventBus):
    def __init__(self):
        self.events = []

    def emit(self, type: str, **data) -> None:
        self.events.append((type, data))


def _cfg(tmp_path: Path, **sim) -> Config:
    data = copy.deepcopy(BASE.raw())
    data["repair"]["runs_dir"] = str(tmp_path / "runs")
    data["cameras"]["calibration"] = str(tmp_path / "no_camera.json")  # the default pose, on every machine
    if sim:
        data["sim"] = sim
    return Config(data)


def _scene() -> Scene:
    return Scene(objects=(BLUE,), t=0.0, source="test")


def _blue_centroid(rgb):
    r, g, b = (rgb[..., i].astype(int) for i in range(3))
    mask = (b - np.maximum(r, g)) > 60
    assert mask.sum() > 20, "blue block not visible in the render"
    vs, us = np.nonzero(mask)
    return float(us.mean()), float(vs.mean())


@pytest.mark.parametrize("principal_shift", [(0, 0), (40, -30)])
def test_render_lines_up_with_the_pinhole_projection(tmp_path, principal_shift):
    """An object lands on the same pixel in the sim render as through the camera model,
    including an off-centre principal point (real calibrations have one)."""
    cam = CameraPose.look_at((0.22, 0.10, 0.70), (0.22, 0.10, 0.0), (1.0, 0.0, 0.0), (640, 480), 50.0)
    cam.K[0][2] += principal_shift[0]
    cam.K[1][2] += principal_shift[1]
    world = build_world(_scene(), MJCF, camera=cam)
    rec = SimRecorder(world, tmp_path / "runs", video=False)
    try:
        u, v = _blue_centroid(rec.frame(world.make_data()))
    finally:
        rec.close()
    eu, ev = cam.project((BLUE.x, BLUE.y, BLUE.top))
    assert abs(u - eu) < 3 and abs(v - ev) < 3, ((u, v), (eu, ev))


def test_default_camera_sees_the_object_where_it_projects(tmp_path):
    cam = overhead_camera(_cfg(tmp_path))
    world = build_world(_scene(), MJCF, camera=cam)
    rec = SimRecorder(world, tmp_path / "runs", video=False)
    try:
        rgb = rec.frame(world.make_data())
    finally:
        rec.close()
    assert rgb.shape == (cam.size[1], cam.size[0], 3)
    u, v = _blue_centroid(rgb)
    eu, ev = cam.project((BLUE.x, BLUE.y, BLUE.z + BLUE.height / 2))
    assert abs(u - eu) < 8 and abs(v - ev) < 8, ((u, v), (eu, ev))


@pytest.fixture
def attempt(tmp_path):
    cfg = _cfg(tmp_path)
    env = make_env("sim", cfg, _scene())
    try:
        yield env, run_skill(PICK, env, {"obj": BLUE.id}, cfg, _Bus(), skill="pick_test")
    finally:
        env.close()


def test_attempt_has_an_mp4_under_the_runs_dir(attempt, tmp_path):
    _, result = attempt
    assert result.error is None, result.error
    path = Path(result.video_path)
    assert path.is_file() and path.suffix == ".mp4"
    assert (tmp_path / "runs" / "sim") in path.parents
    frames = list(iio.imiter(path))
    assert len(frames) >= 10                          # several seconds of sim time at VIDEO_FPS
    assert frames[0].shape[1] <= 480 and frames[0].shape[0] % 2 == 0
    assert not np.array_equal(frames[0], frames[-1])  # the arm moved


def test_video_path_is_stable_until_the_scene_resets(attempt):
    env, result = attempt
    assert env.arm.last_video_path == result.video_path
    env.arm.reset_scene()
    assert env.arm.last_video_path is None


def test_keyframes_per_phase_next_to_the_video(attempt):
    env, result = attempt
    trace = result.trace
    assert [c.name for c in trace] == ["release", "move_to", "move_to", "grasp", "move_to"]
    phases = [PhaseCheck("approach", True, "", 0, 2), PhaseCheck("grasp", True, "", 3, 3),
              PhaseCheck("lift", True, "", 4, 4)]
    frames = env.arm.keyframes(trace, phases)
    assert set(frames) == {"approach", "grasp", "lift"}
    for phase, path in frames.items():
        assert Path(path).parent == Path(result.video_path).parent and Path(path).name == f"{phase}.jpg"
    imgs = {p: iio.imread(f).astype(int) for p, f in frames.items()}
    assert imgs["approach"].shape == (480, 640, 3)
    lift = float(np.abs(imgs["lift"] - imgs["grasp"]).mean())
    assert lift > 0.5, lift  # lifting moves the arm in the overhead view


def test_snapshot_saves_the_current_overhead_render(attempt, tmp_path):
    env, _ = attempt
    out = env.perception.snapshot(str(tmp_path / "snap" / "now.jpg"))
    assert out is not None and Path(out).is_file()
    assert iio.imread(out).shape == (480, 640, 3)


def test_video_can_be_turned_off_but_keyframes_stay(tmp_path):
    cfg = _cfg(tmp_path, video=False)
    env = make_env("sim", cfg, _scene())
    try:
        result = run_skill(PICK, env, {"obj": BLUE.id}, cfg, _Bus(), skill="pick_test")
        frames = env.arm.keyframes(result.trace, [PhaseCheck("grasp", True, "", 3, 3)])
    finally:
        env.close()
    assert result.video_path is None
    assert Path(frames["grasp"]).is_file()


def test_no_camera_means_no_frames_and_a_reported_reason(tmp_path):
    world = build_world(_scene(), MJCF)  # no camera
    with pytest.warns(RuntimeWarning, match="rendering disabled"):
        rec = SimRecorder(world, tmp_path / "runs")
    data = world.make_data()
    rec.tick(data)
    rec.mark(data)
    assert not rec.ok and "overhead" in rec.render_error
    assert rec.finish() is None and rec.keyframes([], [PhaseCheck("grasp", True, "", 0, 0)]) == {}
    assert rec.snapshot(data, str(tmp_path / "x.jpg")) is None
    assert not (tmp_path / "runs").exists()
