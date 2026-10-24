# WS3c: perception track C, world model & wrist

**Branch prefix:** `ws3c/` · **Owns:** `src/skillsmith/perception/world.py`, `src/skillsmith/perception/wrist.py` (new, WS3.7), `tests/ws3c/` · **Reads:** `docs/NORTH_STAR.md`, `docs/workstreams/WS3_perception_world.md` (shared design notes: read first), `docs/INTERFACES.md` §1–2, `docs/ARCHITECTURE.md` (world state)

## Goal
The live WorldModel: stable object ids at 10–15 Hz, held objects follow the gripper, hidden
objects keep their last position, bumps raise `WorldChanged`, and the confidence gate tells the
classifier when a failure is the environment's fault. Plus the wrist-camera checks.
Serves NORTH_STAR: 1, 3, 4.

## Tasks (one PR each, in order)
- [ ] WS3.5 WorldModel: live tracking at 10-15 Hz, stable ids, held object follows the gripper, hidden objects keep last position, bump detection
- [ ] WS3.6 Perception confidence gate: marker drift, low confidence and object-count changes reported to the classifier
- [ ] WS3.7 Wrist camera: post-grasp holding check and fine-alignment offset for align_over

## Acceptance
- Unit tests feed synthetic detections (positions already in the robot frame) through a fake
  detector and fake camera model: held-follows-gripper, hidden keeps position, bump fires once with
  a readable message, `perception_ok()` flips on drift / low confidence / count change.
- `WorldModel` satisfies the `Perception` protocol and drops into the fake env.
- Never import track A or B internals beyond the stub signatures; use fakes (`tests/ws3/ws3_fakes.py`
  is readable, copy what you need into `tests/ws3c/`).
