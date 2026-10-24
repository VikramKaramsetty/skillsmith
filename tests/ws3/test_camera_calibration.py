"""WS3.2: camera calibration (checkerboard intrinsics, ArUco pose) against exact synthetic truth. Heavy job."""

import json

import pytest
from ws3_fakes import FIXTURES

from skillsmith.perception.frames import FixtureFrames
from skillsmith.perception.geometry import (
    CalibrationError,
    CalibrationMissing,
    CameraModel,
    calibrate,
    calibrate_intrinsics,
    calibrate_pose,
)

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

TRUTH = json.loads((FIXTURES / "truth.json").read_text())
CAM = TRUTH["camera"]
K, R, T = (np.array(CAM[k], float) for k in ("K", "R", "t"))
MARKERS = {int(k): tuple(v) for k, v in TRUTH["markers"].items()}
PATTERN, SQUARE = (9, 6), 0.025  # inner corners, square edge (m)


def fixture_bgr(name):
    source = FixtureFrames(FIXTURES / "truth.json")
    for _ in TRUTH["frames"]:
        frame = source.read()
        if frame.source == f"fixture:{name}":
            return frame.bgr()
    raise KeyError(name)


def board_views(n=12):
    """The printed checkerboard held at n tilted poses in front of the fixture camera (exact K)."""
    cols, rows = PATTERN[0] + 1, PATTERN[1] + 1
    px = 40
    board = np.full((rows * px + 2 * px, cols * px + 2 * px), 255, np.uint8)
    for r in range(rows):
        for c in range(cols):
            if (r + c) % 2 == 0:
                board[px + r * px:px + (r + 1) * px, px + c * px:px + (c + 1) * px] = 0
    m_per_px = SQUARE / px
    h, w = board.shape
    rng = np.random.default_rng(3)
    views = []
    for _ in range(n):
        rvec = rng.uniform(-0.35, 0.35, 3)
        centre = (w * m_per_px / 2, h * m_per_px / 2)  # keep the whole board in view
        tvec = np.array([rng.uniform(-0.03, 0.03) - centre[0], rng.uniform(-0.03, 0.03) - centre[1],
                         rng.uniform(0.55, 0.7)])
        corners = np.array([[0, 0, 0], [w, 0, 0], [w, h, 0], [0, h, 0]], float) * m_per_px
        img_pts, _ = cv2.projectPoints(corners, rvec, tvec, K, None)
        H = cv2.getPerspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]), np.float32(img_pts.reshape(-1, 2)))
        view = cv2.warpPerspective(board, H, tuple(CAM["size"]), borderValue=150)
        views.append(cv2.cvtColor(view, cv2.COLOR_GRAY2BGR))
    return views


def test_pose_from_markers_matches_the_true_camera():
    model = calibrate_pose(fixture_bgr("apart.jpg"), K.tolist(), CAM["dist"], MARKERS, TRUTH["marker_size"])
    assert np.allclose(model.R, R, atol=2e-3)
    assert np.allclose(model.t, T, atol=3e-3)
    assert model.reprojection_px < 1.0


@pytest.mark.parametrize("scene", ["apart.jpg", "stacked.jpg", "edges.jpg", "stacked_far.jpg"])
def test_calibrated_camera_projects_each_object_top_where_it_is_seen(scene):
    model = calibrate_pose(fixture_bgr(scene), K.tolist(), CAM["dist"], MARKERS, TRUTH["marker_size"])
    for o in next(f for f in TRUTH["frames"] if f["file"] == scene)["objects"]:
        assert model.project(o["x"], o["y"], o["z"] + o["height"]) == pytest.approx(o["top_uv"], abs=1.5)


def test_markers_re_detected_on_the_table_within_a_centimetre():
    model = calibrate_pose(fixture_bgr("edges.jpg"), K.tolist(), CAM["dist"], MARKERS, TRUTH["marker_size"])
    errors = model.marker_errors(fixture_bgr("edges.jpg"))
    assert set(errors) == set(MARKERS)
    assert max(errors.values()) < 0.01


def test_pixel_to_table_is_the_inverse_of_project():
    model = calibrate_pose(fixture_bgr("apart.jpg"), K.tolist(), CAM["dist"], MARKERS, TRUTH["marker_size"])
    u, v = model.project(0.25, -0.05, 0.08)
    assert model.pixel_to_table(u, v, 0.08) == pytest.approx((0.25, -0.05), abs=1e-6)


def test_intrinsics_from_checkerboard_views():
    K_est, dist, rms = calibrate_intrinsics(board_views(), PATTERN, SQUARE)
    assert K_est[0][0] == pytest.approx(K[0, 0], rel=0.02)
    assert K_est[1][1] == pytest.approx(K[1, 1], rel=0.02)
    assert K_est[0][2] == pytest.approx(K[0, 2], abs=6)
    assert K_est[1][2] == pytest.approx(K[1, 2], abs=6)
    assert rms < 1.0


def test_too_few_checkerboard_views_is_refused():
    with pytest.raises(CalibrationError, match="checkerboard"):
        calibrate_intrinsics([fixture_bgr("apart.jpg")] * 3, PATTERN, SQUARE)


def test_missing_markers_are_named():
    frame = fixture_bgr("apart.jpg")
    frame[:, :] = 150  # nothing to see
    with pytest.raises(CalibrationError, match=r"0, 1, 2, 3"):
        calibrate_pose(frame, K.tolist(), CAM["dist"], MARKERS, TRUTH["marker_size"])


def test_calibrate_saves_and_loads_the_contract_schema(tmp_path):
    model = calibrate({"checkerboard": board_views(), "markers": fixture_bgr("stacked.jpg")}, MARKERS,
                      {"pattern": PATTERN, "square": SQUARE}, marker_size=TRUTH["marker_size"])
    path = model.save(tmp_path / "camera.json")
    data = json.loads(path.read_text())
    assert {"K", "dist", "R", "t", "markers", "created"} <= set(data)
    assert data["markers"] == {str(k): list(v) for k, v in MARKERS.items()}
    loaded = CameraModel.load(path)
    assert loaded.project(0.2, 0.0, 0.0) == pytest.approx(model.project(0.2, 0.0, 0.0))
    assert loaded.project(0.22, 0.08, 0.16) == pytest.approx(
        next(o for o in TRUTH["frames"][1]["objects"] if o["id"] == "blue_cup_0")["top_uv"], abs=3)


def test_operator_tool_calibrates_then_checks(tmp_path, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("calibrate_camera", FIXTURES.parents[2] / "tools" / "calibrate_camera.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    shots = tmp_path / "shots"
    shots.mkdir()
    for i, view in enumerate(board_views()):
        cv2.imwrite(str(shots / f"{i:02d}.png"), view)
    (tmp_path / "markers.json").write_text(json.dumps(TRUTH["markers"]))
    out = tmp_path / "camera.json"
    common = ["--frame", str(FIXTURES / "edges.jpg"), "--out", str(out)]
    assert tool.main(["--board", str(shots), "--markers", str(tmp_path / "markers.json"), *common]) == 0
    assert tool.main(["--check", *common]) == 0
    printed = capsys.readouterr().out
    assert printed.count("OK") == 2 and "marker 3:" in printed

    moved = dict(TRUTH["markers"], **{"2": [0.40, 0.21]})  # someone moved marker 2 by 4 cm
    model = CameraModel.load(out)
    model.markers = moved
    model.save(out)
    assert tool.main(["--check", *common]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_a_missing_calibration_says_how_to_make_one(tmp_path):
    with pytest.raises(CalibrationMissing, match="tools/calibrate_camera.py"):
        CameraModel.load(tmp_path / "camera.json")
