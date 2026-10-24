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

| You are… | Read |
|---|---|
| Setting up a new computer | **`SETUP.md`** (10 minutes, then `scripts/agent-loop.sh all`) |
| A human on the team | this file, then `docs/BUILD_PLAN.md`, `docs/DEMO.md` |
| The lead agent | `docs/ORCHESTRATION.md` |
| Running agents on another machine | `docs/MULTI_MACHINE.md` (`scripts/agent-loop.sh ws2`) |
| A workstream agent | `AGENTS.md`, `docs/INTERFACES.md`, your `docs/workstreams/WS*.md` |
| Touching hardware | `docs/RUNBOOK_GX10.md`, `docs/SAFETY.md` |
| Curious why we chose X | `docs/HACKATHON_INFRA.md`, `docs/RESEARCH.md` |

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
docs/                     architecture, contracts, safety, plan, demo, research
src/skillsmith/
  types.py                shared dataclasses (the contract)
  primitives.py           Robot/Scene facade: the ONLY API a skill can touch
  safety.py               workspace / speed / call-budget envelope
  verify.py               success predicates over a Scene
  events.py               event bus → runs/<id>/events.jsonl → dashboard
  hal/                    backends: fake (done), sim (MuJoCo), real (robotd client)
  perception/             colour detection, camera→table homography
  skills/                 validator (AST), runtime (sandboxed exec), library/
  repair/                 Droid client + the self-repair state machine
  cli.py                  `skillsmith ...` entry point
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
| **What Droid can do** | Edit one file (`skill.py`) in a workspace outside the repo, and call our tools: `validate_skill`, `simulate_skill` (sim only), `get_scene`, `list_library`, `read_library_skill`. |
| **What Droid can't do** | Run shell commands (disabled), edit any other file (refused by our permission handler, with the reason sent back), or touch the real arm (never a tool). |
| **What Droid returns** | The edited skill plus a structured result: diagnosis, change, sim_checked, confidence, lesson. |
| **What happens next** | We re-run static → sim → safety ourselves, a human approves, the real arm retries, the camera verifies. Success saves a new skill version and Droid's lesson (`skills/lessons.md`). |
| **How you can see it** | Every Droid message, tool call, tool result, permission decision, diff and result is an event in `runs/<id>/events.jsonl` and on the dashboard. |

## Tracks we are aiming at

- **Best Use of the Factory Droid SDK**: see "Where and how Droid is used" above and the
  criteria checklist in `docs/NORTH_STAR.md`.
- **Most Technically Complex**: agent + simulation + vision + real hardware.
- **Best Hardware Hack**: the arm is the point.
