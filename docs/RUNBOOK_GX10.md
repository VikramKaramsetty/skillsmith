# GX10 + SO-ARM101 runbook

Facts as of 2026-10-24. Hardware steps here are **human-only** (see `AGENTS.md`).

## Hardware

- **Arms:** SO-ARM101 leader + follower, 12× Feetech STS3215, IDs 1–6 each, 1 Mbaud,
  CH343 USB-serial. Follower 12 V, leader ~5.5 V.
  Joint order: 1 shoulder_pan, 2 shoulder_lift, 3 elbow_flex, 4 wrist_flex, 5 wrist_roll,
  6 gripper.
- **Compute:** ASUS Ascent GX10 (GB10, 128 GB unified), Ubuntu 24.04 aarch64, driver 580,
  CUDA 13.0. Host `gx10-3d68`, user `asus`. Public Wi-Fi drops sometimes.
- **Serial (stable paths):**
  - follower `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5AE6057566-if00`
  - leader `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5AE6055440-if00`
- **Cameras:**
  - wrist: `/dev/v4l/by-id/usb-Sonix_Technology_Co.__Ltd._USB2.0_CAM1_USB2.0_CAM1-video-index0`, 640×480@30
  - front (stand-in): MacBook MJPEG at `http://100.67.242.86:8001/stream`, started on the
    Mac with `cam_stream.py --camera 0 --host 100.67.242.86 --port 8001`

## Access

- `ssh asus@100.103.96.17` (Tailscale SSH, no password). Mac Tailscale IP 100.67.242.86.
  Traffic goes through a relay (125–235 ms, a few MB/s).
- Direct link: USB-Ethernet adapter; GX10 `Wired connection 3` is link-local only. It was
  last plugged into the GX10 side.

## Software on the GX10

- `~/robot/.venv`: LeRobot 0.6.2 (editable `~/robot/lerobot`), torch 2.11.0+cu130,
  `[pi,feetech]`. Use this for robotd, teleop, calibration and IK (placo via LeRobot).
- `~/robot/.venv-hq`: patched LeRobot for the hqfang pi05 checkpoint only.
- Checkpoints in `~/robot/checkpoints/` (`pi05_base`, `pi05_so100_101`): not used by
  Skillsmith.
- Calibration: `~/.cache/huggingface/lerobot/calibration/robots/so_follower/follower.json`
  (id `follower`, gripper `range_min` 1415 → 1390; original in
  `follower.backup-20261024.json`) and `.../teleoperators/so_leader/leader.json`.
  LeRobot units: joints in degrees centred on each calibrated range (`use_degrees=True`);
  gripper 0..100.
- Skillsmith code lives in `~/skillsmith/` (`make sync-gx10` from the Mac).

## Teleop (stop robotd first)

```bash
ssh -t asus@100.103.96.17 '~/robot/.venv/bin/lerobot-teleoperate --robot.type=so101_follower --robot.port=/dev/serial/by-id/usb-1a86_USB_Single_Serial_5AE6057566-if00 --robot.id=follower --robot.max_relative_target="{shoulder_pan: 10, shoulder_lift: 10, elbow_flex: 10, wrist_flex: 10, wrist_roll: 10, gripper: 100}" --teleop.type=so101_leader --teleop.port=/dev/serial/by-id/usb-1a86_USB_Single_Serial_5AE6055440-if00 --teleop.id=leader'
```

## Gotchas

- **One process per serial port or camera.** Teleop, tests, streams and robotd conflict.
  Check with `pgrep -a python` before starting anything.
- **Never `pkill -f <pattern>` over Tailscale SSH**: it matches the SSH session's own
  command line. Use `pgrep -a <name>` and then `kill <pid>`.
- **Disconnecting turns torque off and the arm goes limp. Support it.**
- Before enabling torque from a script, set Goal_Position = Present_Position.
- `max_relative_target` / `max_step_deg`: 5–10° works; 2° let the elbow stall and creep
  under gravity.
- The previous boot ended with NVIDIA "bad register read" errors. Check
  `sudo dmesg | grep -i nvrm` at the start of each session.
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
4. **Homography.** Place 4 ArUco markers at known (x, y) on the table (touch each with the
   gripper tip and read FK, which beats a ruler). Run the WS3 calibration command to write
   `config/homography.json`. Check: detected marker positions within 1 cm.
5. **Detection.** Tune HSV for the demo objects under the demo lighting; `look()` must
   report each object within 1.5 cm of its true position.
6. **Fake → sim → real.** `pick_place` on `--env fake`, then `--env sim`, then
   `--env real` with `max_tcp_speed: 0.05`, operator's hand near the stop. Move a block to
   a taped target; repeat 3×. Pass = within 2 cm every time.

## Droid on the GX10

```bash
curl -fsSL https://app.factory.ai/cli | sh       # check Factory docs for the current installer
export FACTORY_API_KEY=...                        # put it in ~/.bashrc on the GX10, never in the repo
droid exec --list-tools --auto low                # confirm "Execute" exists before removing it
droid exec "reply with ok"                        # auth + network check
```

If the GX10's Wi-Fi drops, run Droid on the Mac instead: set `droid_ws_root` to a folder
synced both ways over the Ethernet link (or run the orchestrator on the Mac with
`--env real` pointing `robotd_url` at the GX10's link-local address; robotd then has to
listen on that interface too, not just 127.0.0.1, and only on that interface).
