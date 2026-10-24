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

## Tracks we are aiming at

- **Factory Droid**: Droid fires on a trigger we designed (task failure), runs with
  controlled permissions (`--auto low`, edits one file, AST + sim + safety gates), and
  every step shows on the dashboard.
- **Most Technically Complex**: agent + simulation + vision + real hardware.
- **Best Hardware Hack**: the arm is the point.
