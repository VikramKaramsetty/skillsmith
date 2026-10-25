# stack-v1 (2026-10-25): first two-cup stack that worked on the real arm

Confirmed by the supervisor ("it did stack"): `stack_cups` left the cup on the T4 side where it was, picked the
second cup with a light side grip (jaws stopped at 30.1 %), lifted it ~12 cm, carried it level around the
base (not across it) at tip z 0.20, set it on the first cup and let go; it nested (run 20261025-083043-429d).

- `pick_cup.py`, `place_cup.py`, `stack_cups.py`: the skills as they ran (from `skills/scratch/library`).
- `camera.json`: the overhead calibration they ran with (re-posed from the mat markers, all within 0.21 cm).
- Code + config are git tag `stack-v1`: cup grip `gripper_closed_by_kind: {cup: 29}`, holding = jaws
  >= 1 % short of the command with load >= 50; a held or level move keeps its grip tilt (the tilt cap
  follows it); the perception gate ignores detection doubt while a cup is still in the jaws.

Revert to it:

    git checkout stack-v1                            # code + config (or: git checkout stack-v1 -- src config robotd)
    cp skills/snapshots/stack-v1/{pick_cup,place_cup,stack_cups}.py skills/scratch/library/
    cp skills/snapshots/stack-v1/camera.json config/camera.json   # only if the camera has not moved

Run it (one cup on T4, the other in reach and > 12 cm away, arm home):

    uv run -q --extra dev --extra dashboard --extra kin --extra vision --extra sim --extra droid skillsmith --profile slow,scratch run "stack cups at T4" --env real --skill stack_cups --params '{"x": 0.28, "y": -0.10}' --success '{"op": "near", "args": ["pink cup", 0.28, -0.10]}' --author droid --approver cli
