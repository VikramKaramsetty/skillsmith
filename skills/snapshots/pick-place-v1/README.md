# pick-place-v1 (2026-10-25): first pick + place that worked on the real arm

Confirmed by the supervisor: `place_cup` picked the pink cup at T4 (0.28, -0.10), carried it level and
set it down on T5 (0.28, 0.10) (run 20261025-073724-7547), after T4 from mid-mat (run 20261025-073423-cb07).

- `pick_cup.py`, `place_cup.py`: the skills as they ran (from `skills/scratch/library`).
- `camera.json`: the overhead iPhone calibration they ran with (checkerboard intrinsics, f = 2043 px).
- Code + config (`config/robot.yaml`: taught grip `gripper_closed_by_kind: {cup: 20}`, gripper torque 250,
  wrist_roll offset -90, side_z_offset 0.02) are git tag `pick-place-v1`.

Revert to it:

    git checkout pick-place-v1                       # code + config (or: git checkout pick-place-v1 -- src config robotd)
    cp skills/snapshots/pick-place-v1/{pick_cup,place_cup}.py skills/scratch/library/
    cp skills/snapshots/pick-place-v1/camera.json config/camera.json   # only if the camera has not moved
    # restart robotd so it reloads robotd/ and config/robot.yaml
