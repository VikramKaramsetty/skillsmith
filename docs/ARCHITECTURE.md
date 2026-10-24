# Architecture

## Layers (code as policies)

| # | Layer | Who writes it | Where it runs | Rate |
|---|---|---|---|---|
| 1 | **Perception**: objects + table positions from camera | us (WS3) | GX10 | on demand (`look()`) |
| 2 | **Skill**: Python that sequences primitives | **Droid** | sandboxed in orchestrator | ~10–30 primitive calls |
| 3 | **Primitives + IK**: Cartesian target → 6 joint angles | us (WS1/WS2) | GX10 | per waypoint |
| 4 | **Motor control**: smooth motion between joint targets | Feetech firmware + our interpolator | servos | 30–50 Hz |

Droid never touches layers 1, 3 or 4. It only writes layer 2, and layer 2 can only call
the `robot` / `scene` / `skills` objects described in `docs/INTERFACES.md`. All the
robotics risk lives in code we write and test once.

## Processes

```
┌──────────────────────────── GX10 (gx10-3d68) ────────────────────────────┐
│                                                                           │
│  robotd  (sole owner of follower serial + wrist cam + front-cam reader)   │
│    ├── HTTP/JSON on 127.0.0.1:8100: joints, move_joints, gripper, frame   │
│    └── enforces joint limits + per-step limit + e-stop (last line)        │
│                                                                           │
│  orchestrator  (`skillsmith serve`)                                       │
│    ├── task intake  ← dashboard                                           │
│    ├── skill runtime (AST-validated, restricted globals)                  │
│    ├── backends: sim (MuJoCo, in-process)  |  real (robotd client)        │
│    ├── safety envelope + verify predicates                                │
│    ├── repair loop → Droid SDK session in ~/.skillsmith/droid_ws        │
│    ├── event bus → runs/<run_id>/events.jsonl                             │
│    └── dashboard server :8080 (SSE events, MJPEG proxy, approve button)   │
│                                                                           │
│  vision model (optional, WS3 stretch): open-vocab detector on the GPU     │
└───────────────────────────────────────────────────────────────────────────┘
         ▲ Tailscale (125–235 ms) or USB-Ethernet link-local (≈1 ms)
┌──────── MacBook ────────┐
│ browser → dashboard     │
│ cam_stream.py (front    │
│ camera stand-in)        │
└─────────────────────────┘
```

Why one `robotd`: only one process can hold a serial port or camera, and teleop, tests,
streams and scripts kept colliding. `robotd` owns the hardware and everyone else is a
client. It is also the last safety layer: it clamps per-step joint deltas and refuses
moves outside joint limits, whatever the caller sends.

## Backends

All three implement the same `ArmBackend` + `Kinematics` + `Perception` protocols
(`docs/INTERFACES.md`), so a skill runs unchanged on each.

| Backend | Purpose | Status |
|---|---|---|
| `fake` | Pure-Python point-object world. Unit tests, CI, `demo-fake`, dashboard development. | **done** in scaffold |
| `sim` | MuJoCo with `robotstudio_so101` (MuJoCo Menagerie) plus a table and cups built from the latest real `Scene`. Physics, collisions, rendered replay. | WS2 |
| `real` | Client of `robotd`; IK via placo (LeRobot `RobotKinematics`); perception from cameras. | WS1 + WS3 |

## The self-repair loop (state machine)

```
             ┌─────────┐
  task ─────▶│ PLAN    │ pick a library skill, or ask Droid to draft one
             └────┬────┘
                  ▼
             ┌─────────┐     success     ┌──────────┐
             │ ATTEMPT │────────────────▶│ SAVE     │ library + registry
             │ (real)  │                 └──────────┘
             └────┬────┘
           fail   │  (verify predicate false, exception, or safety stop)
                  ▼
             ┌─────────┐  failure report (trace, exception, scene diff, snapshot)
             │ REPAIR  │  Droid SDK session (autonomy LOW, our tools) → edits skill.py only
             └────┬────┘
                  ▼
             ┌─────────┐  fail → back to REPAIR with the reason (max N rounds)
             │ STATIC  │  AST validator: approved calls only, no imports, bounded loops
             └────┬────┘
                  ▼
             ┌─────────┐  fail → back to REPAIR with sim trace + verify result
             │ SIM     │  MuJoCo run on a scene built from the real snapshot
             └────┬────┘
                  ▼
             ┌─────────┐  fail → back to REPAIR
             │ SAFETY  │  workspace box, speed, call budget, collisions, IK reachability
             └────┬────┘
                  ▼
             ┌─────────┐  human presses Approve on the dashboard (or rejects with a note)
             │ APPROVE │
             └────┬────┘
                  ▼
               ATTEMPT (real) again
```

Repair rounds are capped (`repair.max_rounds` in config). Each round continues the same
Droid SDK session (kept open for the run), so Droid remembers what it already tried.
A human "reject with note" becomes the next round's failure report.

## Where the Droid runs and what it sees

The orchestrator creates `~/.skillsmith/droid_ws/<run_id>/` from `droid_template/`:

```
AGENTS.md            rules for writing skills (copied from droid_template)
API.md               the primitive API (copied from droid_template)
skill.py             the skill under repair (the ONLY file Droid may edit)
failure.md           what went wrong: exception, trace table, verify result, safety report
snapshot.jpg         camera frame at failure (when available)
scene.json           detected objects
library/*.py         read-only copies of saved skills, to call or imitate
```

Droid runs through the **Droid Python SDK** (`repair/droid_sdk.py`): one `droid_sdk.Session`
per run with `cwd` set to the workspace, kept open across repair rounds so Droid remembers
what it tried. The session has autonomy LOW, `Execute` disabled, and our `skillsmith` SDK
MCP server (`validate_skill`, `simulate_skill`, `get_scene`, `list_library`,
`read_library_skill`). A permission handler (`droid_tools.decide`) allows only edits to
`skill.py` and calls to our tools; everything else is refused with the reason sent back.
Each round returns a JSON-schema result (diagnosis, change, sim_checked, confidence,
lesson). We read back `skill.py` and re-run static, sim and safety ourselves, so Droid cannot
skip or weaken them. The workspace sits outside the repo so Droid never sees the build-time
`AGENTS.md`. Details and the judging checklist: `docs/NORTH_STAR.md`.

## Perception

- **Front camera** (MacBook stand-in now; a fixed overhead USB webcam on the GX10 is
  better): ArUco markers at 4 known table points → homography from pixels to table
  (x, y). Objects sit on the table, so z = known object height. This is the main
  source of object positions.
- **Wrist camera** (Sonix, 640×480@30): optional fine alignment just before a grasp,
  and a "did it grip" check.
- **Detector**: HSV colour blobs first (fast, robust for coloured cups and blocks).
  Stretch: an open-vocabulary detector on the GX10 GPU for "any object".
- **Verify**: re-detect after the attempt and evaluate the task's success predicate,
  e.g. `on_top("blue cup", "red cup")`: xy distance < 3 cm and z above.

## Kinematics

- URDF/MJCF: TheRobotStudio `so101_new_calib` (zero = middle of each joint's range,
  which should match LeRobot's `use_degrees=True` calibrated degrees). **Verify signs
  and offsets on the real arm in the first test** before trusting it.
- IK: LeRobot `RobotKinematics` (placo), target frame `gripper_frame_link`, position
  weight 1.0, small orientation weight. Primitives use a top-down grasp: the gripper
  points down; wrist_roll is set from the object's yaw or left alone.
- Motion: joint-space interpolation at ≤ `safety.max_joint_speed_dps`, plus straight
  Cartesian segments in ≤ 2 cm steps for the final approach.
- Do **not** use the hqfang pi05 joint convention here. That correction (signs
  `[1,-1,1,1,1,1]`, offsets `[0,90,90,0,0,0]`) is for the VLA checkpoint only.

## Data and persistence

- `runs/<run_id>/events.jsonl`: every event, for dashboard replay and post-mortems.
- `runs/<run_id>/attempt_<n>/`: skill version, trace, snapshot, sim video (mp4).
- `skills/library/<name>.py` + `skills/registry.json`: saved skills with version and
  success count. Commit them to git after the demo for history.
