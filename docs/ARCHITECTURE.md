# Architecture

How Skillsmith works. *Why* each choice was made is in `docs/DECISIONS.md`; the exact
contracts are in `docs/INTERFACES.md`; the objective is in `docs/NORTH_STAR.md`.

## Layers (code as policies)

| # | Layer | Written by | Runs on | Rate |
|---|---|---|---|---|
| 1 | **World model**: objects, positions, held/hidden, bump events | us (WS3) | Mac | 10–15 Hz |
| 2 | **Skill**: Python sequencing primitives | **Droid** (and seed skills by us) | orchestrator, sandboxed | ~10–30 calls |
| 3 | **Primitives + IK**: target → joint angles, closed-loop alignment | us (WS2, lead) | Mac | per waypoint |
| 4 | **Motor control**: robotd clamps + servo firmware | us (WS1) + Feetech | Mac + servos | 30–50 Hz |
| — | **Failure analysis**: phases, class, bucket | us (WS4), plain rules | orchestrator | after each attempt |
| — | **Repair**: Droid via the SDK, with evidence and tools | Droid + us (WS5) | orchestrator + Factory | on failure |

Droid only ever writes layer 2. The robotics risk lives in layers 1, 3 and 4, which we write and test once.

## Processes

```
┌───────────────────────── MacBook (one machine) ─────────────────────────┐
│ robotd (only owner of serial port + cameras)                            │
│   USB serial → follower arm; iPhone overhead (Continuity Camera) +      │
│   USB wrist camera, both opened by name                                 │
│   HTTP 127.0.0.1:8100: state (joints, gripper pos+load), move, gripper, │
│   stop, frames/streams (overhead + wrist, timestamped, ~15 fps)         │
│   enforces joint limits, per-step clamp, watchdog (last safety layer)   │
│                                                                         │
│ orchestrator (`skillsmith serve`)                                       │
│   WorldModel thread: frames → detector → ray-cast → tracks → events     │
│   RepairLoop: attempt → analyse → route → Droid → gates → approve       │
│   skill runtime (AST-checked, stripped builtins, SafetyGuard)           │
│   sim check env (MuJoCo, built from the live scene)                     │
│   DroidSDKAuthor: one droid_sdk.Session per run + skillsmith tools      │
│   EventBus → runs/<id>/events.jsonl + dashboard (:8080, SSE)            │
└─────────────────────────────────────────────────────────────────────────┘
        │ internet
        ▼
   Factory (cloud): Droid's model calls
   Frontier vision API (optional, advisory)
```

## The loop

```
 task ──▶ PLAN: library skill, or Droid drafts one (drafts go through the gates first)
            │
            ▼
        ATTEMPT on the real arm ──success──▶ SAVE skill vN+1 + lesson, count success
            │ failure (exception, WorldChanged, safety stop, or the success check fails)
            ▼
        ANALYSE (plain rules, ms): phases + failure class + bucket + confidence
            │
   ┌────────┼───────────────────────────┐
   ▼        ▼                           ▼
 skill    unknown (flagged)        environment
   │        │                           │  camera moved, out of reach, perception
   └───┬────┘                           │  uncertain, hardware, calibration drift
       ▼                                ▼
   REPAIR: Droid (SDK) with evidence v2   HUMAN: fix the setup / recalibrate, then retry
       ▼                                  (Droid is not called)
   STATIC → SIM → SAFETY   (we re-run all of them; any failure → new evidence, same session)
       ▼
   APPROVE (human, dashboard) → reset scene → ATTEMPT again
```

Rounds are capped (`repair.max_rounds`; 2 on stage). If the same failure class repeats after a
value-only change (Droid's `change_type: parameter`), the next evidence says to **change the
approach**.

## Perception and world state (WS3)

1. **Camera calibration** (once, about 15 min): a printed checkerboard for the intrinsics, then
   4+ ArUco markers taped at table points whose robot-frame positions are measured by touching
   them with the gripper tip (FK). Saved to `config/camera.json`.
2. **Detection**: YOLO-World / YOLO-E on the Mac (main), HSV colour blobs (backup). Each
   detection gives a label, the pixel of the object's top centre, and a confidence.
3. **Ray casting**: the top-centre pixel's ray is intersected with the plane at the object's
   known top height (from the object table). Stacked objects use the height of what is below
   them. This removes the parallax a table-plane homography would leave (about 2 cm for a cup top).
4. **WorldModel** (10–15 Hz): stable ids by nearest-neighbour association. The **held object
   follows the gripper** (FK) instead of being re-detected. Objects hidden behind the arm keep
   their last position with `visible=False`. A not-held object moving more than 1 cm sets
   `unexpected_motion()`, and the Robot raises **`WorldChanged`** before the next motion.
5. **Confidence gate**: markers still where calibrated, detection confidence, object count
   stable. If it fails, the classifier must use an environment class, never a skill class.
6. **Wrist camera**: a post-grasp holding check (is the object between the fingers?) and the
   fine x/y offset for `align_over`.
7. **Frames for evidence**: a key frame per phase, annotated (boxes, labels, planned-target
   crosshair, projected gripper tip, error arrow in cm), plus cropped before/after pairs.

## Motion (lead + WS2)

- IK: placo on `so101_new_calib.urdf`, gripper pointing down, with the same class in sim and on
  the real arm. Joint signs and offsets are verified on the arm (WS2.7) before trusting it.
- Vertical moves are split into 2 cm steps; joint speed is capped; the SafetyGuard checks every
  target before motion.
- **`robot.align_over(obj, hover)`** is closed-loop: re-read the object's latest position, hover
  above it, apply the wrist-camera correction if available. Small x/y errors are corrected
  *inside* the attempt, with no restart and no Droid.

## Failure analysis (WS4)

`analysis.analyse_attempt(attempt, cfg) -> (phases, classification)`, called by the runtime
after every attempt.

- **Phases** come from the trace: approach (moves before the first grasp) → grasp → lift (first
  rise while holding) → carry (horizontal while holding) → place (descent while holding) →
  release → retreat. Each gets ok/detail.
- **Classification** uses only measured signals: the failing call and exception type, gripper
  position/load, `holding()` results, scene before/after from the world model, the success
  check's numbers, safety issues, `perception_ok()`, and wrist checks.
- **Buckets** (`types.FAILURE_CLASSES`): *skill* (e.g. `SLIP_IN_CARRY`, `GRASP_MISS`,
  `COLLISION`, `PLACE_OFF_TARGET`, `KNOCKED_ON_APPROACH`), *environment* (e.g. `CAMERA_MOVED`,
  `OBJECT_OUT_OF_REACH`, `PERCEPTION_UNCERTAIN`, `CALIBRATION_DRIFT`, `HARDWARE_FAULT`),
  *unknown*.
- **Output** leads Droid's evidence and the dashboard banner, for example:
  `SLIP_IN_CARRY at call #8 (carry): grasp at z=0.07 (88% of cup height) held, lost during a
  16 cm sideways move. Look at: grip height.`
- Rules fire only on unambiguous evidence; otherwise `UNKNOWN` with a lower confidence.

## Droid integration (WS5)

- **SDK**: `DroidSDKAuthor` keeps one `droid_sdk.Session` per run, open across rounds (memory),
  with `cwd` set to a workspace outside the repo and an **explicit tool allowlist**: only
  `ToolSearch`, `Script`, `WaitForScript`, `TodoWrite` and our `skillsmith` functions. No file,
  shell, web, automation or sub-agent tools. Droid reads and writes only through `read_file` /
  `write_skill` (workspace only), and its fix also arrives as `skill_source`, which our code
  writes. Autonomy is HIGH only so it never pauses for approval; nothing it can reach is
  dangerous, and `make droid-sandbox-check` proves escapes are blocked. The permission handler
  (`droid_tools.decide`) stays as a backstop.
- **Evidence v2** (prompt + `failure.md` + images): failure class and phase → phase table →
  annotated key frames → cropped before/after → expected-vs-actual (sim render or the last
  successful run) → the success check's numbers → scene tables → trace with the failing line →
  earlier rounds and reviewer notes → `LESSONS.md` → library skills.
- **Tools**: `validate_skill`, `simulate_skill` (budgeted), `get_scene`, `list_library`,
  `read_library_skill`. Planned: `get_frame(phase)`, `get_object_track(name)`,
  `compare_with_success(phase)`, so Droid fetches more evidence like a human scrubbing video.
- **Result** (JSON schema): `diagnosis`, `change`, `change_type` (parameter | strategy),
  `sim_checked`, `confidence`, `lesson`.
- **Lessons** go into `skills/lessons.md`, and every later prompt includes them.

## Verification

- **Geometry decides**: predicates (`on_top`, `inside`, `near`, ...) on world-model positions,
  with numeric details ("blue cup is 4.1 cm from red cup, limit 3.0 cm").
- **Vision explains, optionally**: a frontier vision API on cropped before/after returns
  pass/fail plus one sentence. Disagreeing with a geometric pass counts as a fail and adds
  evidence. It never overrides geometry.

## Backends

| Backend | Purpose |
|---|---|
| `fake` | Pure Python. Unit tests, CI, `make demo-fake`. Reproduces slip, collision and miss for classifier tests. |
| `sim` | MuJoCo with the Menagerie SO101 and objects from the live scene. Gate for every Droid change; renders a video and per-phase frames from the calibrated camera pose. |
| `real` | robotd client + PlacoKinematics + WorldModel. Human-run only. |

## Persistence

- `runs/<id>/events.jsonl`: every event (the dashboard renders only from this; the replay fallback uses it).
- `runs/<id>/attempt_<n>/`: skill version, trace, key frames, sim video.
- `skills/library/*.py`, `skills/registry.json`, `skills/history/`, `skills/lessons.md`.
- `config/camera.json`, `calibration/offsets.json`: per-machine, gitignored.
