# WS3a: perception track A, geometry & frames

**Branch prefix:** `ws3a/` · **Owns:** `src/skillsmith/perception/frames.py`, `src/skillsmith/perception/geometry.py`, `tools/calibrate_camera.py`, `tests/ws3/` (shared fixtures), `tests/ws3a/` · **Reads:** `docs/NORTH_STAR.md`, `docs/workstreams/WS3_perception_world.md` (shared design notes: read first), `docs/INTERFACES.md` §1–2, `docs/DECISIONS.md` (3D positions)

## Goal
Turn a pixel into a position in the robot frame, accurate to ~1 cm, and keep the frame sources
reliable. Tracks B and C consume `CameraModel` and the frame readers; keep their signatures.
Serves NORTH_STAR: 1, 3.

## Tasks (one PR each, in order)
- [ ] WS3.2 Camera calibration: checkerboard intrinsics plus ArUco pose in the robot frame, saved to config/camera.json
- [ ] WS3.3 Ray-plane localisation: pixel to (x, y) at an object's known height, with stacked-object z inference
- [ ] WS3.11 MjpegFrames.read() raises on stale frames (max_age_s)
- [ ] WS3.9 Accuracy test: cups on 5 taped marks under demo lighting, under 1 cm error (hardware)

## Acceptance
- `CameraModel.calibrate()` on synthetic checkerboard/ArUco images recovers a known pose; `load()`
  round-trips `config/camera.json` (gitignored; tests write to a tmp path).
- `pixel_to_table()` error < 1.5 cm on the fixture frames against `tests/ws3/fixtures/truth.json`,
  and ray casting beats a plain homography on a stacked cup. `marker_drift()` reports moved markers.
- cv2/numpy imported lazily; cv2 tests run in the heavy CI job.
