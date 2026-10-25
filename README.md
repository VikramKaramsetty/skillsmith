# Skillsmith — a robot arm that writes its own skills

Code-as-policies on an SO-ARM101. The arm knows five safe primitives. Factory Droid
writes and repairs short Python skills that combine them. Every repair is checked
statically, simulated in MuJoCo, safety-checked, and approved by a human before the
real arm moves. Skills that work are saved, so the arm keeps getting more capable.

```
judge task ─▶ try skill ─▶ camera verifies ─▶ FAIL ─▶ Droid edits skill
                  ▲                                         │
                  │        approve ◀─ safety ◀─ MuJoCo ◀─ AST check
                  └──────── real arm ◀──────────┘
```

## Start here

**Everyone starts with [`docs/NORTH_STAR.md`](docs/NORTH_STAR.md)**: the objective, the award
criteria and the decision rule. It wins over every other doc.

| You are… | Read next |
|---|---|
| Setting up a new computer | **`SETUP.md`** (10 minutes, then `scripts/agent-loop.sh all`) |
| Anyone on the team | `docs/PLAN.md` (gates, time budget, cut list), `docs/DEMO.md` (the 2-minute demo) |
| A workstream agent | `AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/INTERFACES.md`, your `docs/workstreams/*.md` |
| The lead | `docs/WORKFLOW.md` (shared queue, CI, contract changes) |
| Touching hardware | `docs/RUNBOOK.md`, `docs/SAFETY.md` |
| Wanting proof it works | `docs/RESULTS.md` (measured with real Droid: 8/8 seeded bugs fixed in one round, median 15 s, with no hint where the bug is; demo repair; sandbox check) |
| Asking "why X?" | `docs/DECISIONS.md` (decisions, hard truths, sources) |

## Quick start (no hardware)

```bash
make setup
make check       # lint + tests
make demo-fake   # full fail → repair → verify loop in a pure-Python fake world
```

## Layout

```
AGENTS.md / CLAUDE.md     rules for agents building this repo
.claude/agents/           one subagent definition per workstream
droid_template/           what the RUNTIME Droid sees (its own AGENTS.md)
config/robot.yaml         ports, cameras, workspace box, speed limits
docs/                     north star, architecture, interfaces, plan, workflow, decisions, demo, safety, runbook
  workstreams/            one brief per workstream (task lines become GitHub issues)
robotd/                   WS1: the only process that owns the arm's serial port and cameras
src/skillsmith/
  types.py                shared dataclasses + failure classes (the contract)
  primitives.py           Robot facade: the ONLY API a skill can touch (incl. align_over, bump stop)
  safety.py               workspace / speed / call-budget envelope
  verify.py               success predicates (geometry decides)
  analysis/               WS4: phase checks + failure classifier (skill / environment / unknown)
  events.py               event bus → runs/<id>/events.jsonl → dashboard
  hal/                    backends: fake (done), sim (MuJoCo), real (robotd client)
  kinematics.py           WS2: placo IK/FK shared by sim and real
  perception/             WS3: camera geometry (ray casting), detectors, live WorldModel, annotated frames
  skills/                 validator (AST), runtime (sandboxed exec)
  repair/                 WS5: Droid SDK author, tools + permission policy, evidence, the repair loop
  dashboard/              WS6: big-screen view, approvals, replay
  calibration/            WS7: offsets for repeated position errors
  cli.py                  `skillsmith ...` entry point
skills/library/           saved skills (+ registry.json, lessons.md)
tests/                    run with `make test`
```

## Where and how Droid is used

Factory Droid is the part of the running product that **writes and repairs the robot's
skills**. It runs through the **Droid Python SDK** (`droid-sdk`), in
[`src/skillsmith/repair/droid_sdk.py`](src/skillsmith/repair/droid_sdk.py). The objective and
judging criteria live in [`docs/NORTH_STAR.md`](docs/NORTH_STAR.md).

| | |
|---|---|
| **Trigger** | The robot's own failure: after a real attempt the camera check fails (also: a gate rejects Droid's code, a human rejects with a note, a new task arrives). `RepairLoop` then calls Droid. No chat box. |
| **What Droid gets** | An evidence package: the task and success check, why it failed (with numbers), scene before/after, the full motion trace with the failing line, cropped before/after images plus key frames from the attempt, earlier rounds, lessons from past tasks, and the saved skill library. |
| **What Droid can do** | Change one file (`skill.py`, in a workspace outside the repo) only through our `write_skill` tool, and call our other tools: `read_file` (its workspace only), `validate_skill`, `simulate_skill` (sim only), `get_scene`, `list_library`, `read_library_skill`, and the evidence tools `get_frame`, `get_object_track`, `compare_with_success`. |
| **What Droid can't do** | Anything outside an explicit allowlist: no shell, no file access outside its workspace, no network, no automations or sub-agents, never the real arm. `make droid-sandbox-check` proves it live. |
| **What Droid returns** | The new skill (`skill_source`) plus a structured result: diagnosis, change, change_type (parameter or strategy), sim_checked, confidence, lesson. |
| **What happens next** | We re-run static → sim → safety ourselves, a human approves, the real arm retries, the camera verifies. Success saves a new skill version and Droid's lesson (`skills/lessons.md`). |
| **What it needs** | The Droid Python SDK (`droid-sdk`, `--extra droid`), the `droid` CLI it drives, and `FACTORY_API_KEY` from https://app.factory.ai/settings/api-keys (an account with credits). Setup: `SETUP.md`. |
| **How you can see it** | Every Droid message, tool call, tool result, permission decision, diff and result is an event in `runs/<id>/events.jsonl` and on the dashboard. |

## Tracks we are aiming at

- **Best Use of the Factory Droid SDK**: see "Where and how Droid is used" above and the
  criteria checklist in `docs/NORTH_STAR.md`.
- **Most Technically Complex**: agent + simulation + vision + real hardware.
- **Best Hardware Hack**: the arm is the point.
