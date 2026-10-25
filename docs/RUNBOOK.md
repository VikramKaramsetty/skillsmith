# Mac + SO-ARM101 runbook

Facts as of 2026-10-24. Everything runs on one Mac: robotd, the arm, both cameras, sim,
dashboard and Droid. Hardware steps here are **human-only** (see `AGENTS.md`).

## Hardware

- **Arms:** SO-ARM101 leader + follower, 12× Feetech STS3215, IDs 1–6 each, 1 Mbaud,
  CH343 USB-serial. Follower 12 V, leader ~5.5 V.
  Joint order: 1 shoulder_pan, 2 shoulder_lift, 3 elbow_flex, 4 wrist_flex, 5 wrist_roll,
  6 gripper.
- **Compute:** MacBook Pro M5 Pro, 48 GB, macOS 26.
- **Serial ports:**
  - follower `/dev/cu.usbmodem5AE60575661` (CH343 serial 5AE6057566)
  - leader `/dev/cu.usbmodem5AE60554401` (CH343 serial 5AE6055440), plugged in only for
    teleop and calibration
  - Missing? Run `ls /dev/cu.*`, plug the arm in, run it again; the new entry is the arm.
- **Cameras** (robotd opens both by name):
  - front (overhead): an iPhone through Continuity Camera, `name:iPhone`. See
    "iPhone overhead camera" below.
  - wrist: the USB Sonix camera (USB2.0_CAM1), 640×480@30.
  - With robotd stopped, `uv run --extra vision python tools/list_cameras.py` saves one frame
    per camera to `runs/camera-probe/`. Allow camera access for your terminal app when macOS asks.

## Software on the Mac

- `~/robot/.venv`: `lerobot[feetech]==0.6.1` from PyPI, plus fastapi, uvicorn, opencv-python,
  pyyaml and imageio-ffmpeg. Use it for robotd, teleop and calibration.
- robotd, from the repo root: `~/robot/.venv/bin/python -m robotd`.
- Everything else (orchestrator, sim, dashboard, Droid) runs from this repo with `uv`
  (`make setup`).
- LeRobot calibration lives in `~/.cache/huggingface/lerobot/calibration/`
  (`robots/so_follower/follower.json`, `teleoperators/so_leader/leader.json`).
  LeRobot units: joints in degrees centred on each calibrated range (`use_degrees=True`);
  gripper 0..100.

## Arm calibration (once on this Mac, about 10 min per arm)

The old calibration files stayed on the GX10, which we no longer use, so recalibrate once.
Stop robotd first.

```bash
~/robot/.venv/bin/lerobot-calibrate --robot.type=so101_follower --robot.port=/dev/cu.usbmodem5AE60575661 --robot.id=follower
~/robot/.venv/bin/lerobot-calibrate --teleop.type=so101_leader --teleop.port=/dev/cu.usbmodem5AE60554401 --teleop.id=leader
```

Move each joint through its full range when LeRobot asks. Then check the gripper closes fully:
on the old setup the follower's gripper `range_min` had to drop from 1415 to 1390.

## Teleop (stop robotd first)

```bash
~/robot/.venv/bin/lerobot-teleoperate --robot.type=so101_follower --robot.port=/dev/cu.usbmodem5AE60575661 --robot.id=follower --robot.max_relative_target="{shoulder_pan: 10, shoulder_lift: 10, elbow_flex: 10, wrist_flex: 10, wrist_roll: 10, gripper: 100}" --teleop.type=so101_leader --teleop.port=/dev/cu.usbmodem5AE60554401 --teleop.id=leader
```

## iPhone overhead camera

- **Continuity Camera:** iPhone and Mac on the same Apple ID, with Wi-Fi and Bluetooth on.
  The iPhone is locked, on power, mounted in landscape, high and centred over the table.
- A USB cable from the iPhone to the Mac is more stable than wireless.
- **Video effects off** in Control Center while robotd has the camera open: Center Stage,
  Portrait, Studio Light and Desk View. They crop and zoom, which breaks the camera calibration.
- **Recalibrate the camera** (`tools/calibrate_camera.py`, step 4 below) after mounting it and
  after any bump.

## Gotchas

- **One process per serial port or camera.** Teleop, calibration, tests, `list_cameras.py`
  and robotd conflict. Check with `pgrep -a python` before starting anything; stop one with
  `kill <pid>`.
- **Disconnecting turns torque off and the arm goes limp. Support it.**
- Before enabling torque from a script, set Goal_Position = Present_Position.
- `max_relative_target` / `max_step_deg`: 5–10° works; 2° let the elbow stall and creep
  under gravity.
- Keep the Mac on power and awake during real runs (`caffeinate -dims` in a spare terminal).
- The pi05 joint convention (signs `[1,-1,1,1,1,1]`, offsets `[0,90,90,0,0,0]`) is for the
  hqfang VLA only. **Do not use it with the URDF/IK.**

## G3 first-test procedure (hand-scripted pick-and-place)

1. **Joint signs and offsets.** With robotd running and torque **off**, read `/state` while
   moving the arm by hand to the folded rest pose and to a "straight up" pose. Compare
   LeRobot degrees with the URDF's joint zero (mid-range) for each joint. Then, torque on
   at `max_step_deg: 5`, jog one joint at a time +10° and confirm the URDF predicts the
   same direction. Record any per-joint sign/offset in `config/robot.yaml` (lead's file:
   send it to the lead).
2. **FK vs ruler.** Put the tip at 3–4 points over the taped grid; compare
   `kin.fk(joints)` with ruler measurements in the base frame. Target error ≤ 1 cm. If
   it's worse, fix step 1 before going further.
3. **Workspace box.** Jog to the edges you are comfortable with and update
   `workspace` and `home_xyz` in config.
4. **Camera calibration** (WS3.2, about 15 min). Mount the iPhone high and centred over the
   workspace, fix it in place, video effects off. Show the printed checkerboard in ~15 poses
   (intrinsics). Tape 4+ ArUco markers at table points and touch each with the gripper tip to
   read its robot-frame (x, y) from FK. Run `python tools/calibrate_camera.py` to write
   `config/camera.json`. Check: re-detected marker positions within 1 cm. Redo after any bump.
5. **Detection + world model.** Tune HSV (backup) and check YOLO-World under the demo lighting;
   the world model must report each cup within 1 cm (WS3.9), and nudging a cup by hand must
   raise `WorldChanged` within ~0.2 s. Also run the full signal checklist in `docs/PLAN.md`
   (gripper holding vs empty, slip visible, placement error).
6. **Fake → sim → real.** `pick_place` on `--env fake`, then `--env sim`, then
   `--env real` with `max_tcp_speed: 0.05`, operator's hand near the stop. Move a block to
   a taped target; repeat 3×. Pass = within 2 cm every time.

## Droid on the Mac

```bash
curl -fsSL https://app.factory.ai/cli | sh       # official installer (checksum-verified, ~/.local/bin)
echo 'export FACTORY_API_KEY=<key>' >> ~/.zshrc   # key from https://app.factory.ai/settings/api-keys; never in the repo
source ~/.zshrc
droid exec "Reply with ok" -o json                # must show "is_error":false (402 = no plan/credits on the account)
uv run --extra droid skillsmith demo-fake --author droid   # live check of OUR SDK path (DroidSDKAuthor) on the fake world
```

The product uses the **Droid Python SDK** (`droid-sdk`), which drives the `droid` CLI and reads
`FACTORY_API_KEY`. The orchestrator inherits the variable from the shell that starts it.
