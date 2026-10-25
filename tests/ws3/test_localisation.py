"""WS3.3 acceptance: fixture frame -> HSV detector -> calibrated ray casting -> WorldModel, against truth.json.

Heavy job (cv2). The calibration is recovered from each frame's ArUco markers (WS3.2), never copied
from the truth, so these tests cover the whole chain the real arm will run.
"""

import json
import math

import pytest
from ws3_fakes import FIXTURES

from skillsmith.config import Config, load_config
from skillsmith.perception.frames import FixtureFrames
from skillsmith.perception.geometry import calibrate_pose

pytest.importorskip("cv2")
pytest.importorskip("numpy")
from skillsmith.perception.color import HsvDetector  # noqa: E402
from skillsmith.perception.world import WorldModel  # noqa: E402

LIMIT_M = 0.015  # WS3 acceptance: position error under 1.5 cm
TRUTH = json.loads((FIXTURES / "truth.json").read_text())
SCENES = {f["file"]: f for f in TRUTH["frames"]}
def _fixture_config():
    """robot.yaml without the rig's measured objects and detector: the recorded fixtures use 8 cm cups."""
    data = load_config().raw()
    data = {**data, "world": {k: v for k, v in (data.get("world") or {}).items() if k != "objects"}}
    data.pop("detect", None)
    return Config(data)


CFG = _fixture_config()


class Sequence:
    """Fixture frames in a chosen order; the last one repeats (the scene holds still)."""

    def __init__(self, tmp_path, *names):
        manifest = tmp_path / ("_".join(names) + ".json")
        manifest.write_text(json.dumps({"frames": [{"file": str(FIXTURES / n), "t": SCENES[n]["t"]} for n in names]}))
        self._source = FixtureFrames(manifest)
        self._last = None

    def read(self):
        try:
            self._last = self._source.read()
        except Exception:
            if self._last is None:
                raise
        return self._last

    def close(self):
        pass


def camera_for(name):
    frame = FixtureFrames(FIXTURES / "truth.json")
    bgr = next(f for f in (frame.read() for _ in TRUTH["frames"]) if f.source == f"fixture:{name}").bgr()
    cam = TRUTH["camera"]
    return calibrate_pose(bgr, cam["K"], cam["dist"], {int(k): v for k, v in TRUTH["markers"].items()},
                          TRUTH["marker_size"])


def world(tmp_path, *names, steps=3):
    model = WorldModel(CFG, frame_source=Sequence(tmp_path, *names), detector=HsvDetector(CFG),
                       camera=camera_for(names[-1]))
    for _ in range(len(names) - 1 + steps):
        model.step()
    return model.observe()


def nearest(scene, truth_obj):
    same = [o for o in scene.objects if o.label == truth_obj["label"]]
    return min(same, key=lambda o: math.hypot(o.x - truth_obj["x"], o.y - truth_obj["y"]), default=None)


def error_m(found, truth_obj):
    return math.hypot(found.x - truth_obj["x"], found.y - truth_obj["y"])


@pytest.mark.parametrize("name", ["apart.jpg", "edges.jpg", "stacked_far_before.jpg"])
def test_every_object_is_found_within_the_limit(tmp_path, name):
    scene = world(tmp_path, name)
    for o in SCENES[name]["objects"]:
        found = nearest(scene, o)
        assert found is not None, f"{o['label']} not found in {name}"
        assert error_m(found, o) < LIMIT_M, (name, o["id"], error_m(found, o))
        assert found.z == pytest.approx(o["z"], abs=1e-6)


@pytest.mark.parametrize("before, stacked", [("apart.jpg", "stacked.jpg"), ("stacked_far_before.jpg", "stacked_far.jpg")])
def test_a_cup_put_on_a_tracked_cup_is_located_at_the_stacked_height(tmp_path, before, stacked):
    scene = world(tmp_path, before, stacked)
    top = next(o for o in SCENES[stacked]["objects"] if o["id"] == "blue_cup_0")
    found = nearest(scene, top)
    assert found is not None
    assert found.z == pytest.approx(top["z"], abs=0.005)  # inferred: it sits on the red cup
    assert error_m(found, top) < LIMIT_M


def test_ray_casting_beats_the_plain_table_homography_on_the_far_stack(tmp_path):
    camera = camera_for("stacked_far.jpg")
    top = next(o for o in SCENES["stacked_far.jpg"]["objects"] if o["id"] == "blue_cup_0")
    u, v = top["top_uv"]
    homography_xy = camera.pixel_to_table(u, v, 0.0)  # what a table-plane homography reports
    found = nearest(world(tmp_path, "stacked_far_before.jpg", "stacked_far.jpg"), top)
    assert math.hypot(homography_xy[0] - top["x"], homography_xy[1] - top["y"]) > 0.035
    assert error_m(found, top) < LIMIT_M
