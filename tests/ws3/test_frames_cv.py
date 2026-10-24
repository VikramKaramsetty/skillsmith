"""WS3.1: decoding, the OpenCV source and the fixture frames' ground truth (heavy job: needs cv2/numpy)."""

import json
import shutil

import pytest
from ws3_fakes import FIXTURES

from skillsmith.perception.frames import FixtureFrames, FrameError, OpenCVFrames

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

TRUTH = json.loads((FIXTURES / "truth.json").read_text())
CAM = TRUTH["camera"]
K, R, T = (np.array(CAM[k], float) for k in ("K", "R", "t"))
SCENES = {f["file"]: f for f in TRUTH["frames"]}
DOMINANT = {"blue": 0, "green": 1, "red": 2}  # BGR channel


def frames():
    source = FixtureFrames(FIXTURES / "truth.json")
    return [source.read() for _ in TRUTH["frames"]]


def project(xyz):
    p = K @ (R @ np.asarray(xyz, float) + T)
    return p[:2] / p[2]


def ray_to_plane(uv, z):
    """Where the pixel's ray meets the plane at height z (robot frame)."""
    centre = -R.T @ T
    direction = R.T @ np.linalg.inv(K) @ np.array([uv[0], uv[1], 1.0])
    s = (z - centre[2]) / direction[2]
    return (centre + s * direction)[:2]


def test_fixture_frames_decode_to_the_camera_size():
    assert [f.bgr().shape for f in frames()] == [(CAM["size"][1], CAM["size"][0], 3)] * len(TRUTH["frames"])


def test_a_corrupt_jpeg_is_a_frame_error():
    from skillsmith.perception.frames import Frame

    with pytest.raises(FrameError, match="decod"):
        Frame(b"\xff\xd8garbage", 0.0, "test").bgr()


def test_truth_top_pixels_agree_with_the_camera_model():
    for scene in TRUTH["frames"]:
        for o in scene["objects"]:
            assert project((o["x"], o["y"], o["z"] + o["height"])) == pytest.approx(o["top_uv"], abs=0.01)


def covered(o, objects):
    """Something rests on o's top, so its top centre is not visible."""
    return any(p is not o and abs(p["z"] - (o["z"] + o["height"])) < 1e-6
               and np.hypot(p["x"] - o["x"], p["y"] - o["y"]) < o["width"] / 2 for p in objects)


def test_each_visible_object_shows_its_colour_at_its_top_centre():
    for frame, scene in zip(frames(), TRUTH["frames"]):
        img = frame.bgr()
        for o in (o for o in scene["objects"] if not covered(o, scene["objects"])):
            u, v = (int(round(c)) for c in o["top_uv"])
            patch = img[v - 2:v + 3, u - 2:u + 3].reshape(-1, 3).mean(axis=0)
            assert int(np.argmax(patch)) == DOMINANT[o["color"]], (scene["file"], o["id"], patch)


def test_every_calibration_marker_is_detectable():
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50),
                                       cv2.aruco.DetectorParameters())
    for frame in frames():
        _, ids, _ = detector.detectMarkers(frame.bgr())
        assert sorted(ids.flatten().tolist()) == sorted(int(k) for k in TRUTH["markers"])


@pytest.mark.parametrize("scene, min_parallax", [
    ("stacked.jpg", 0.015),  # the demo stack, ~7 cm off-centre: already past WS3's 1.5 cm limit
    ("stacked_far.jpg", 0.035),  # ~17 cm off-centre: D7's ~4 cm
])
def test_a_stacked_cup_needs_ray_casting_not_a_table_homography(scene, min_parallax):
    top = next(o for o in SCENES[scene]["objects"] if o["id"] == "blue_cup_0")
    truth_xy = np.array([top["x"], top["y"]])
    on_table = ray_to_plane(top["top_uv"], 0.0)  # what a table-plane homography would report
    at_height = ray_to_plane(top["top_uv"], top["z"] + top["height"])
    assert np.linalg.norm(on_table - truth_xy) > min_parallax
    assert np.linalg.norm(at_height - truth_xy) < 0.002


def test_opencv_source_reads_frames_as_jpegs(tmp_path):
    for i, name in enumerate(SCENES):
        shutil.copy(FIXTURES / name, tmp_path / f"img_{i:03d}.jpg")
    source = OpenCVFrames(str(tmp_path / "img_%03d.jpg"))
    try:
        frame = source.read()
    finally:
        source.close()
    assert frame.jpeg[:2] == b"\xff\xd8"
    assert frame.bgr().shape == (480, 640, 3)
    assert frame.source.startswith("opencv:")


def test_opencv_source_that_cannot_open_says_so(tmp_path):
    with pytest.raises(FrameError, match="cannot open"):
        OpenCVFrames(str(tmp_path / "missing_%03d.jpg"))
