# WS3: perception

**Branch prefix:** `ws3/` · **Owns:** `src/skillsmith/perception/`, `tools/hsv_tuner.py`, `tools/calibrate_camera.py`, `tests/ws3/`
**Reads:** `docs/INTERFACES.md` §1 (Scene/SceneObject) and §2 (Perception), `docs/ARCHITECTURE.md` "Perception"

## Goal
`CameraPerception.observe() -> Scene` with object positions in the robot frame, accurate
to about 1 cm on the table, fast (< 200 ms), from a fixed front/overhead camera.

## Tasks (one PR each, in order)
- [ ] WS3.1 Frame sources (robotd /frame client, OpenCV device, MJPEG URL) plus recorded sample frames as test fixtures
- [ ] WS3.2 ArUco table calibration tool that writes a pixel-to-table homography
- [ ] WS3.3 HSV colour detector that returns a Scene, with a known-object size table and stacked-z inference
- [ ] WS3.4 HSV tuner tool with live sliders that saves thresholds
- [ ] WS3.5 CameraPerception class implementing observe() and snapshot()
- [ ] WS3.6 Measure position accuracy on the real table at marked points (hardware)
- [ ] WS3.7 YOLO-World / YOLO-E open-vocabulary detector on the GX10 as the main detector, HSV as backup
- [ ] WS3.8 Frontier-API vision success check (advisory): cropped before/after images in, pass/fail plus one sentence out, never overrides geometry
- [ ] WS3.9 Post-grasp wrist-camera holding check that returns a specific reason (with WS4.6)

## Design notes
- **Three layers (docs/NORTH_STAR.md "The camera check"):** *detect* with YOLO-World / YOLO-E
  (fast open-vocabulary, the main detector; Grounding DINO / OWLv2 are much slower) with HSV
  as the backup; *decide* with the geometric predicates in `verify.py` (the only pass/fail
  authority); *explain*: Droid reasons about the failure itself. WS3.8 adds an optional
  second opinion from a **frontier vision API** (not a local VLM) on cropped before/after
  images, which returns pass/fail plus one sentence. A "fail" from it against a geometric
  "pass" counts as a fail, and its sentence goes into Droid's evidence package. Measure the
  latency (expect a few seconds); read the API key from an env var and never commit it.
- Crop to the objects named in the success predicate before asking any vision model; full
  frames make vision judges noticeably worse. WS5.10 reuses the same crops for Droid's prompt.
- The post-grasp check (WS3.9) uses the wrist camera: object pixels between the fingers,
  plus the gripper position from robotd. It returns `(holding: bool, reason: str)`.
- Objects sit on the table plane, so a homography (4+ ArUco markers at known robot-frame
  points) gives (x, y) with no depth. Measure the marker positions by touching them with
  the gripper tip and reading FK (needs WS2), or with a ruler from the base axis.
- For cups seen at an angle, use the blob's bottom-centre, not its centroid.
- `z`: 0 on the table. If a blob's footprint overlaps another object's footprint and
  it sits higher in the image, it is stacked: z = the lower object's top.
- Size: look up `height`/`width` by kind from a table (cup 0.08/0.065, block
  0.03/0.03; measure the real objects). Don't estimate size from pixels.
- Stable ids: `<color>_<kind>_<n>`, ordered by x then y.
- Import `cv2` lazily. Fixture frames (WS3.1) make the detector testable in CI.
- The Mac camera stream has 125–235 ms of latency over Tailscale. Fine for snapshots;
  a fixed USB webcam on the GX10 is better (no Wi-Fi in the loop).

## Acceptance (in `make check`, heavy job for cv2 tests)
- On the fixture frames: every expected object found with the right label, and position
  error < 1.5 cm against hand-labelled ground truth in `tests/ws3/fixtures/truth.json`.
- `CameraPerception` satisfies the `Perception` protocol.

## Hardware test (arm operator, WS3.6)
Place a cup on 5 taped marks, run `python tools/calibrate_camera.py --check`, report the
error per mark. Target < 1 cm.
