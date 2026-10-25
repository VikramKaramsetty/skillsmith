# WS1: hardware (robotd + real arm client)

**Branch prefix:** `ws1/` · **Owns:** `robotd/`, `src/skillsmith/hal/real.py`, `tests/ws1/` · **Reads:** `docs/NORTH_STAR.md`, `docs/INTERFACES.md` §2, `docs/RUNBOOK.md`, `docs/SAFETY.md` (layer 7)

## Goal
One process on the Mac, `robotd`, owns the follower's serial port and both cameras and serves
the HTTP API in INTERFACES §2. It is the last safety layer: it enforces joint limits and per-step
deltas whatever the caller sends. `hal/real.py` is a thin client that wires robotd, PlacoKinematics
(WS2) and the WorldModel (WS3) into an `Env`.
Serves NORTH_STAR: 1, 4.

## Tasks (one PR each, in order)
- [ ] WS1.1 robotd service with a mock arm and the full HTTP API, including gripper position and load in /state
- [ ] WS1.2 Feetech backend via LeRobot's SO101 follower (calibrated degrees, Goal=Present before torque-on)
- [ ] WS1.3 Safety clamps in robotd: joint limits, max_step_deg interpolation, /stop, command watchdog
- [ ] WS1.4 Camera streaming in robotd: overhead and wrist frames with timestamps at ~15 fps for the world model
- [ ] WS1.5 RobotdArm client and make_real_env wiring (PlacoKinematics + WorldModel)
- [ ] WS1.6 Smoke test robotd on the Mac (hardware)
- [ ] WS1.7 Gripper signal test: holding vs empty on our real cups, 10 grasps each, report thresholds (hardware)

## Design notes
- FastAPI + uvicorn (`robotd` extra). `--mock` runs a fake arm that tracks joints, so the whole
  API works with no hardware. CI uses mock only. Entry point: `python -m robotd` (stub exists).
- Feetech backend: reuse LeRobot from `~/robot/.venv` (`so_follower`, id `follower`,
  `use_degrees=True`), imported lazily; tests skip it.
- `move_joints` blocks: interpolate in steps ≤ `safety.max_step_deg` (5–10° works; 2° stalled
  the elbow under gravity), wait until Present is within ~1.5° of Goal, else 409 with details.
- `/state` returns joints, gripper Present_Position and Present_Load, `holding`, torque, `t`.
  The `holding` threshold comes from WS1.7 measurements, not a guess; the classifier and the
  post-grasp check depend on it.
- Camera frames carry a capture timestamp so the world model can align them with joint states.
  Cameras are opened by name (the iPhone via Continuity Camera, the USB wrist camera). Only robotd
  opens them; everything else reads frames from robotd.
- Bind `127.0.0.1` by default (everything runs on the same Mac). `--host` plus a shared token
  header exist for a second machine, but the demo doesn't need them.
- Torque-off makes the arm go limp: `/torque {"on": false}` logs a warning and the dashboard
  says "support the arm".

## Acceptance (in `make check`)
- `tests/ws1/` (FastAPI TestClient, mock backend): a large move splits into ≤ max_step_deg steps;
  a target outside the joint limits returns 400 and does not move; `/stop` interrupts a move;
  `/state` has gripper position and load; `RobotdArm` satisfies `ArmBackend` end to end.
- Use `pytest.importorskip("fastapi")` where needed.

## Hardware test (arm operator)
- WS1.6: from the repo root on the Mac, `~/robot/.venv/bin/python -m robotd --port 8100`,
  then `curl localhost:8100/state`, a 5° shoulder_pan move, gripper open/close. Support the arm.
- WS1.7: grasp each demo cup 10× and close on air 10×; record Present_Position and Present_Load;
  report whether "holding" vs "empty" separates cleanly (soft cups may squash: say so).
