# WS2: kinematics + MuJoCo sim

**Branch prefix:** `ws2/` · **Owns:** `src/skillsmith/kinematics.py`, `src/skillsmith/hal/sim.py`, `assets/so101/`, `tests/ws2/` · **Reads:** `docs/NORTH_STAR.md`, `docs/INTERFACES.md` §2, `src/skillsmith/hal/fake.py`, `docs/ARCHITECTURE.md` (kinematics, sim gate)

## Goal
One `Kinematics` implementation shared by the real arm and the sim, and a MuJoCo `Env` built
from the latest real `Scene`, so the sim gate checks the exact code that will run on a copy of
the real table. The sim also renders the "expected" frames Droid compares with reality.
Serves NORTH_STAR: 1, 3, 4.

## Tasks (one PR each, in order)
- [ ] WS2.1 Vendor the SO101 URDF (so101_new_calib) and the Menagerie robotstudio_so101 MJCF with licenses
- [ ] WS2.2 PlacoKinematics: top-down IK and FK with round-trip tests
- [ ] WS2.3 MuJoCo scene builder: Scene objects become cups and blocks on a table
- [ ] WS2.4 make_sim_env with contacts raised as CollisionError and exposed via safety_issues()
- [ ] WS2.5 Render sim attempts: mp4 plus per-phase key frames from the overhead camera pose (for expected-vs-actual)
- [ ] WS2.6 G2 acceptance: pick_place and the demo stack skill in sim
- [ ] WS2.7 Verify URDF joint signs and offsets against the real arm's calibrated degrees (hardware)

## Design notes
- **placo directly**, never LeRobot (it pulls in torch). Same approach as LeRobot's
  `RobotKinematics`: frame `gripper_frame_link`, joints in degrees. Contract stub:
  `kinematics.py` (`PlacoKinematics`, `make_kinematics(cfg)`); import placo lazily.
- Top-down grasp: gripper z-axis down (strong weight), position weight 1.0, wrist_roll fixed
  unless yaw matters. Return None when the residual position error is > 5 mm.
- `new_calib` zero is mid-range, which should match LeRobot `use_degrees=True`; WS2.7 proves it.
  Never apply the hqfang pi05 sign/offset convention here.
- MuJoCo grasping is finicky: when both pads touch one object on `set_gripper(closed)`, attach it
  with a weld constraint. Document this honestly: the sim gate checks geometry, reach and
  collisions, not friction or slip.
- Contacts other than pads↔held object, objects↔table and stacked object↔support are
  collisions: raise `CollisionError` with names and heights, and list them in `arm.safety_issues()`.
- The sim's overhead camera uses the real camera pose from `config/camera.json` (WS3.2), so its
  key frames line up with real frames for expected-vs-actual pairs. `arm.last_video_path` = mp4.
- Headless rendering in CI: `MUJOCO_GL=egl`. Keep video optional so the gate stays fast (< 5 s).

## Acceptance (in `make check`, heavy job)
- `tests/ws2/`: FK→IK round trip < 2 mm over a workspace grid; unreachable points return None;
  `pick_place` and `demo/stack_v2.py` succeed in sim; a lowered place height raises CollisionError
  where the fake world would. Use `pytest.importorskip("mujoco")` / `("placo")`.

## Hardware test (arm operator, WS2.7)
Jog each joint ±20° (teleop off), read robotd `/state`, compare the FK tip with a ruler on the
table. Report sign/offset fixes for `config/robot.yaml` as a CONTRACT CHANGE REQUEST.
