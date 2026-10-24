# WS3: perception & world state

**Split into three parallel tracks** (separate files, separate queues) · **Reads:** `docs/NORTH_STAR.md`, `docs/ARCHITECTURE.md` (perception and world state), `docs/INTERFACES.md` §1–2, `docs/DECISIONS.md` (3D positions)

## Goal
Know where every object is, live, accurately, and say when we're unsure. The WorldModel tracks
objects at 10–15 Hz in the robot frame (~1 cm), notices when something gets bumped, checks grasps
with the wrist camera, and produces the annotated frames Droid needs to see what went wrong.
Serves NORTH_STAR: 1, 3, 4.

## Tracks (each has its own label, queue, branch prefix and files, so three machines build at once)
| Track | Brief | Owns | Tasks |
|---|---|---|---|
| `ws3a` geometry & frames | `WS3A_geometry_frames.md` | `perception/frames.py`, `perception/geometry.py`, `tools/calibrate_camera.py`, `tests/ws3/` (fixtures) | WS3.2, 3.3, 3.9, 3.11 |
| `ws3b` detectors & annotation | `WS3B_detect_annotate.md` | `perception/color.py`, `perception/annotate.py`, `perception/vision.py`, `tools/hsv_tuner.py` | WS3.4, 3.8, 3.10 |
| `ws3c` world model & wrist | `WS3C_world_wrist.md` | `perception/world.py`, `perception/wrist.py` | WS3.5, 3.6, 3.7 |

No track waits on another: each codes against the contract stubs (`CameraModel`, `Detection`,
`WorldModel`) and tests with fakes or fixture frames. `perception/__init__.py` belongs to the lead.
This file keeps the shared design notes; the track briefs hold the task lists.

## Design notes
- Contract stubs exist: `perception/geometry.py` (CameraModel, calibrate), `color.py` (detectors),
  `world.py` (WorldModel), `annotate.py` (key frames, crops). Fill them in; don't rename.
- **Why ray casting, not a homography:** a homography is exact only on the table plane. An
  overhead camera sees a cup's top rim (8 cm up); with the camera 80 cm high and the cup 20 cm
  off-centre that's ~2 cm of parallax (~4 cm for a stacked cup). Intersect each pixel's ray with
  the plane at the object's known top height instead. Mount the camera high and centred.
- Marker positions are measured by touching them with the gripper tip (FK), not with a ruler.
- Detections give the TOP-centre pixel; sizes come from a known-object table, never from pixels.
- WorldModel rules: a held object follows the FK tip; an object hidden behind the arm keeps its
  last position with `visible=False` (never "moved"); a not-held object moving more than
  `world.bump_threshold` (1 cm) → `unexpected_motion()` message → Robot raises WorldChanged.
- Confidence gate (`perception_ok()`): calibration markers moved, detection confidence below
  `world.min_confidence`, or object count changed → the classifier labels it an environment
  problem, never a skill bug.
- **Geometry decides pass/fail; vision only explains.** Crops of the task's objects make any
  vision judge better (and Droid too). WS3.10 API keys come from env vars, never committed.
- Optional robustness trick: ArUco stickers on objects (blocks, upturned cup bottoms) make
  detection nearly free and give orientation, so a tipped object becomes visible.
- Read frames only through robotd (it owns the cameras). Import cv2/numpy/ultralytics lazily.

## Acceptance (in `make check`, heavy job for cv2 tests)
- On fixture frames: every expected object found with the right label; position error < 1.5 cm
  against `tests/ws3/fixtures/truth.json`; ray casting beats the plain homography on stacked cups.
- WorldModel unit tests with synthetic detections: held-follows-gripper, hidden keeps position,
  bump fires once with a readable message. `WorldModel` satisfies `Perception`.

## Hardware test (arm operator, WS3.9)
Cups on 5 taped marks under the demo lighting; run `python tools/calibrate_camera.py --check`;
report error per mark (target < 1 cm), including one stacked pair.
