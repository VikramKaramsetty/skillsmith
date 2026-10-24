# WS2: kinematics + MuJoCo sim

**Branch prefix:** `ws2/` · **Owns:** `src/skillsmith/kinematics.py`, `src/skillsmith/hal/sim.py`, `assets/so101/`, `tests/ws2/`
**Reads:** `docs/INTERFACES.md` §2, `src/skillsmith/hal/fake.py` (reference behaviour), `docs/ARCHITECTURE.md` "Kinematics"

## Goal
(1) One `Kinematics` implementation that the real arm and the sim share. (2) A MuJoCo
`Env` built from a `Scene`, so the sim gate checks the exact code that will run on the
real arm, against a copy of the real table.

## Tasks (one PR each, in order)
- [ ] WS2.1 Vendor the SO101 URDF (so101_new_calib) and the Menagerie robotstudio_so101 MJCF into assets/so101 with licenses
- [ ] WS2.2 PlacoKinematics in kinematics.py: top-down IK and FK on so101_new_calib, with round-trip tests
- [ ] WS2.3 MuJoCo scene builder: Scene objects become cups and blocks on a table in the SO101 scene
- [ ] WS2.4 make_sim_env: SimArm and SimPerception with joint stepping, gripper, holding, and contacts raised as CollisionError
- [ ] WS2.5 Offscreen render of each sim attempt to mp4 (arm.last_video_path) for the dashboard
- [ ] WS2.6 Run pick_place and the demo stack skill in sim as the G2 acceptance test
- [ ] WS2.7 Check joint signs and offsets of the URDF against the real arm's calibrated degrees (hardware)

## Design notes
- Use **placo directly** with the URDF (target frame `gripper_frame_link`), the same
  way LeRobot's `RobotKinematics` does. Do not import LeRobot here (it pulls in torch).
  Joint order: shoulder_pan, shoulder_lift, elbow_flex, wrist_flex, wrist_roll, then the
  gripper copied from the seed.
- Top-down grasp: constrain the gripper z-axis to point down (strong weight), position
  weight 1.0. Fix wrist_roll unless yaw matters. Return None when the position error
  after solving is > 5 mm.
- Units at the interface are degrees and metres. The `new_calib` zero is mid-range,
  which should match LeRobot `use_degrees=True`; WS2.7 verifies this on the arm.
- MuJoCo grasping is finicky. On `set_gripper(closed)`, if both pads touch one object,
  attach it with a weld equality constraint (document this honestly). The sim gate is
  about kinematics, reach and collisions, not friction.
- Contacts: anything other than pads↔held object, objects↔table and stacked
  object↔support counts as a collision. Raise `CollisionError` and also expose
  `arm.safety_issues()`.
- Headless rendering on Linux CI: `MUJOCO_GL=egl` or `osmesa`. Keep video optional.

## Acceptance (in `make check`, heavy job)
- `tests/ws2/`: FK→IK round trip < 2 mm over a grid in the workspace box; unreachable
  points return None; `pick_place` and `demo/stack_v2.py` succeed in sim;
  `demo/stack_v1.py` with a lowered place height raises CollisionError.
- Use `pytest.importorskip("mujoco")` / `("placo")` so the light CI job still passes.

## Hardware test (arm operator, WS2.7)
Jog each joint ±20° with teleop off, read `robotd /state`, compare FK tip position with
a ruler on the table. Record the sign and offset fixes in `config/robot.yaml` (lead).
