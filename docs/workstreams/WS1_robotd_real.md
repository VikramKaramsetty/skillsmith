# WS1: robotd + real arm client

**Branch prefix:** `ws1/` · **Owns:** `robotd/`, `src/skillsmith/hal/real.py`, `tests/ws1/`
**Reads:** `docs/INTERFACES.md` §2 (robotd HTTP API), `docs/RUNBOOK_GX10.md`, `docs/SAFETY.md` layer 7

## Goal
One process on the GX10, `robotd`, owns the follower arm's serial port and the cameras,
and exposes the HTTP API from INTERFACES §2. `hal/real.py` is a thin client of it.
robotd is the last safety layer: whatever the caller sends, it enforces joint limits
and per-step deltas.

## Tasks (one PR each, in order)
- [ ] WS1.1 robotd service skeleton with a mock arm backend and the full HTTP API
- [ ] WS1.2 Feetech backend for robotd using LeRobot's SO101 follower (calibrated degrees, Goal=Present before torque-on)
- [ ] WS1.3 robotd safety clamps: joint limits, max_step_deg interpolation, /stop, command watchdog
- [ ] WS1.4 RobotdArm client and make_real_env wiring in hal/real.py
- [ ] WS1.5 robotd camera endpoints: wrist JPEG/MJPEG and a front-camera proxy
- [ ] WS1.6 Hardware smoke test of robotd on the GX10 (hardware)

## Design notes
- FastAPI + uvicorn (the `robotd` extra). Run with `--mock` for development: a fake arm
  that tracks joints so the whole API works with no hardware. CI uses mock only.
- Feetech backend: reuse LeRobot from `~/robot/.venv` (`lerobot.robots.so_follower`,
  id `follower`, `use_degrees=True`). Import LeRobot lazily; tests skip it.
- `move_joints` blocks: interpolate in steps ≤ `safety.max_step_deg`, send each,
  wait until Present is within 1.5° of Goal or time out (then raise 409 with details).
  Steps that are too small stall under gravity (2° stalled the elbow); 5–10° works.
- `holding`: gripper Present_Position stays well above the commanded closed value
  (an object is in the way) and/or Present_Load is high. Calibrate the threshold on the arm.
- Bind to `127.0.0.1` by default; add `--host` so the Mac fallback (orchestrator on the
  Mac over the USB-Ethernet link) can reach it. If bound beyond localhost, require a
  shared token header.
- Torque-off makes the arm go limp. `/torque {"on": false}` must log a warning, and the
  dashboard should say "support the arm".

## Acceptance (in `make check`)
- `tests/ws1/`: with the mock backend via FastAPI TestClient, a large move is split into
  ≤ max_step_deg steps; targets outside joint limits return 400 and do not move; `/stop`
  interrupts a move; `RobotdArm` satisfies the `ArmBackend` protocol end to end.

## Hardware test (arm operator, WS1.6)
`ssh -t asus@100.103.96.17 'cd ~/skillsmith && ~/robot/.venv/bin/python -m robotd --port 8100'`, then
`curl localhost:8100/state`, a 5° move of shoulder_pan, and gripper open/close. Support the arm.
