# WS3b: perception track B, detectors & annotation

**Branch prefix:** `ws3b/` · **Owns:** `src/skillsmith/perception/color.py`, `src/skillsmith/perception/annotate.py`, `src/skillsmith/perception/vision.py` (new, WS3.10), `tools/hsv_tuner.py`, `tests/ws3b/` · **Reads:** `docs/NORTH_STAR.md`, `docs/workstreams/WS3_perception_world.md` (shared design notes: read first), `docs/INTERFACES.md` §1–2

## Goal
Find every task object in a frame with a label, a top-centre pixel and a confidence, and produce
the annotated key frames and before/after crops that Droid and the dashboard look at.
Serves NORTH_STAR: 3, 4.

## Tasks (one PR each, in order)
- [ ] WS3.4 Detectors behind one interface: YOLO-World/YOLO-E main, HSV backup, with confidence
- [ ] WS3.8 Annotated key frames and cropped before/after pairs for Droid and the dashboard
- [ ] WS3.10 Stretch: frontier-API vision second opinion on cropped before/after (advisory)

## Acceptance
- Both detectors return the same detection type; on `tests/ws3/fixtures/*.jpg` every expected
  object is found with the right label (read the fixtures; track A owns them).
- HSV detector works with no ML deps installed; YOLO import is lazy and its tests skip when absent.
- `annotate_keyframe()` / `crop_pair()` take pixel coordinates (use a stub projection in tests;
  do not import track A's calibration). WS3.10 keys come from env vars, never committed; advisory only.
