# AGENTS.md — Skillsmith build rules

This file is for agents (Claude Code, Factory Droid, or anything else) **building** Skillsmith.
The Droid that writes robot skills **at runtime** uses a different file,
`droid_template/AGENTS.md`, which is copied into a workspace *outside* this repo.
Never merge the two.

## What we are building (one paragraph)

Skillsmith is a robot arm (SO-ARM101) that writes its own skills. A small set of
hand-tested primitives (`move_to`, `grasp`, `release`, `look`, `home`) is exposed to
Python "skills". When a task fails, Factory Droid edits the skill; the skill is
statically checked, run in MuJoCo, safety-checked, approved by a human, then run on the
real arm, and the camera confirms. Working skills are saved and reused.
Read `docs/ARCHITECTURE.md` for the full picture.

## Read before you write code

0. **`docs/NORTH_STAR.md`: the objective, the award criteria and the decision rule. It wins
   over every other doc.** Every task must serve one of its four points; say which in your hand-off.
1. `docs/ARCHITECTURE.md`: layers, processes, data flow.
2. `docs/INTERFACES.md`: **the contracts**. If you need to change one, stop and say so;
   do not change it on your own (see "Contract changes" below).
3. Your workstream file in `docs/workstreams/` (it lists the files you own).
4. `docs/SAFETY.md` if you touch anything that can move the arm.

## Ownership (who may edit what)

Each workstream owns specific paths (listed in its WS file). Edit only your paths plus
your own tests. Shared files (`src/skillsmith/types.py`, `docs/INTERFACES.md`,
`config/robot.yaml`, `pyproject.toml`, `Makefile`) belong to the **lead**; propose
changes in your hand-off note instead.

## Commands

```bash
make setup     # uv venv + install (core + dev)
make check     # ruff + pytest; must pass before you hand off
make test      # pytest only
make demo-fake # end-to-end repair loop on the fake world (no hardware, no MuJoCo, no Droid)
```

Python ≥ 3.10, managed with `uv`. The core package depends only on `pyyaml`. Heavy deps
(mujoco, placo, opencv, fastapi) are optional extras and must be imported lazily
inside the module that needs them, so `make check` runs anywhere.

## Hard rules

- **Never move the real arm.** Agents do not run anything with `--backend real` and do
  not open `/dev/serial/*` or `/dev/video*` on the GX10. Write the code plus a test
  against `fake` or `sim`; a human runs hardware. Say "needs hardware test" in your
  hand-off, with the exact command.
- Never `pkill -f <pattern>` over SSH (the pattern matches the SSH session itself).
  Use `pgrep -a <name>` and then `kill <pid>`.
- Only `robotd` opens the follower serial port and the cameras. Everything else talks
  to `robotd`. (Only one process can hold a serial port or a camera at a time.)
- Units everywhere: metres, seconds, degrees for joints, gripper 0–100 (0 = closed).
  Frame: robot base frame, x forward, y left, z up, origin on the table surface under
  the shoulder_pan axis.
- No new dependencies without the lead's OK.
- Keep functions small and typed. Pass dataclasses from `skillsmith.types` across module
  boundaries, not loose dicts.
- Every runtime failure emits an event (`skillsmith.events`) so the dashboard shows it.
  A silent failure is a bug.

## Contract changes

If an interface in `docs/INTERFACES.md` blocks you, write the proposed change at the
top of your hand-off note under `## CONTRACT CHANGE REQUEST`, keep working against the
current contract with a local adapter, and let the lead merge the change.

## Hand-off note (end every task with this)

```
## Hand-off: <workstream> — <short title>
- Serves (NORTH_STAR decision rule): 1 loop | 2 improvement | 3 control & visibility | 4 safety
- Done: ...
- Tests: `make check` summary line
- Needs hardware test: yes/no — exact command for the human
- Known gaps / TODO: ...
- CONTRACT CHANGE REQUEST: none | <details>
```
