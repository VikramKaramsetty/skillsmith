"""robotd: the ONLY process that opens the follower's serial port and the cameras. OWNER: WS1.

Brief: docs/workstreams/WS1_hardware.md. HTTP API: docs/INTERFACES.md §2.

Run on the GX10:  ~/robot/.venv/bin/python -m robotd --port 8100 [--mock] [--host 127.0.0.1]

Last line of defence (docs/SAFETY.md layer 7): joint limits from the LeRobot calibration,
per-command delta clamp (safety.max_step_deg), Goal=Present before torque-on, /stop.
Develop and test with --mock. Agents never run it without --mock.
"""
