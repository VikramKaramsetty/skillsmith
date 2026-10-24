# Workflow: how we build in parallel

Several machines and accounts run coding agents at once. GitHub is the hub: **issues are the
task queue, pull requests merge themselves when CI is green**. This doc covers the mechanics;
what to build is in `docs/PLAN.md` and the briefs in `docs/workstreams/`.

## Principles

1. **North star first.** Every task serves `docs/NORTH_STAR.md`'s decision rule; hand-offs say which point.
2. **Contracts first.** Shared types and protocols already exist (`types.py`, `hal/base.py`,
   `primitives.py`, `analysis/`, `repair/protocols.py`, `events.py`, `docs/INTERFACES.md`).
   Workstreams build behind them, so nobody waits on anybody.
3. **Fake first.** The `fake` backend and `ScriptedAuthor` run the whole loop in under a second,
   so every workstream tests end to end with no hardware, MuJoCo or Droid.
4. **One owner per file**, enforced in CI by `.github/ownership.yml`.
5. **One task in flight per workstream**, so tasks run in order and agents never collide.
6. **Hardware is a mutex**: only the arm operator runs `needs-hardware` tasks.

## Workstreams

| WS | Name | Brief |
|---|---|---|
| WS1 | Hardware: robotd, real arm, cameras, gripper signals | `workstreams/WS1_hardware.md` |
| WS2 | Kinematics & MuJoCo sim | `workstreams/WS2_kinematics_sim.md` |
| WS3 | Perception & live world state | `workstreams/WS3_perception_world.md` |
| WS4 | Runtime, safety & failure analysis | `workstreams/WS4_runtime_safety_analysis.md` |
| WS5 | Droid loop (SDK, evidence, benchmark) | `workstreams/WS5_droid_loop.md` |
| WS6 | Dashboard | `workstreams/WS6_dashboard.md` |
| WS7 | Skill library & calibration | `workstreams/WS7_library_calibration.md` |
| QA | Integration tests | `workstreams/QA_integration.md` |

## How one task flows

1. Task lines (`- [ ] WS2.3 ...`) in the briefs become GitHub issues (`scripts/seed-issues.sh`),
   labelled with the workstream and `ready` (or `needs-hardware`).
2. Every machine runs `POLL=300 scripts/agent-loop.sh all`. It skips workstreams that already
   have a task `in-progress` or `blocked`, takes the oldest `ready` task elsewhere, and claims
   it with a comment. GitHub orders comments, so exactly one machine wins a race.
3. `scripts/agent-run.sh` creates a worktree on `ws2/<issue>-<slug>` from `origin/main` and runs
   a headless agent (`claude -p`, or `droid exec` with `AGENT=droid`) with the brief.
4. The agent works until `make check` passes and writes `HANDOFF.md`.
5. `scripts/ship.sh` commits, rebases on `main`, re-runs the checks and the ownership check,
   pushes, and opens a PR labelled `automerge` with `HANDOFF.md` as the body.
6. CI runs. Green → the PR merges itself → "Closes #N" closes the issue. Red → the failed log
   goes back to the agent (up to 3 times); then the issue is labelled `blocked`.

## CI (`.github/workflows/ci.yml`)

| Job | What it does | Why |
|---|---|---|
| `check` | `make check`: lint, all tests, static check of every library skill | catches broken code |
| `heavy` | the same tests with MuJoCo, placo, OpenCV and droid-sdk installed | tests that skip without those libraries actually run |
| `ownership` | changed files vs `.github/ownership.yml`, keyed by branch prefix | stops a workstream editing another's files |
| `automerge` | after the three pass: squash-merge and delete the branch | no human merge step; skipped for `contract-change` and `blocked` |

Branch protection on `main` requires `check`, `heavy` and `ownership`.

## Contract changes

1. An agent writes a `CONTRACT CHANGE REQUEST` in its `HANDOFF.md`.
2. The lead makes the change on a `lead/<slug>` branch (code + `docs/INTERFACES.md` + affected
   briefs), labels it `contract-change`, and merges by hand.
3. Running loops pick it up automatically: every task starts from the latest `main`.

## The lead's job (about 10 minutes an hour)

- `make status`: open PRs, in-progress, blocked, ready, the hardware queue, CI on `main`.
- Unblock `blocked` issues: read the PR and `../skillsmith-logs/*.jsonl`, sharpen the issue,
  relabel it `ready`, or take it over.
- Add follow-ups as new task lines in a brief, then re-run `scripts/seed-issues.sh` (it skips
  existing titles).
- If `main` breaks, fix it at once on a `lead/` branch.

## Setting up a machine

`SETUP.md` at the repo root (10 minutes). In short: install the tools, accept the invite, clone,
`make setup && make check`, log in to Claude, then `POLL=300 scripts/agent-loop.sh all` in tmux.
Stop gracefully with `touch .agent-stop`.

## Working by hand

Create a worktree on a `wsN/...` branch, work in Claude Code (the `.claude/agents/wsN-*`
subagents know their lane), write `HANDOFF.md`, then `scripts/ship.sh "wsN: what I did" <issue>`.

## Failure modes

| Symptom | Fix |
|---|---|
| `ship.sh` exits 2 (rebase conflict) | resolve in the worktree, `git rebase --continue`, re-run ship |
| `ownership` red | move the edit to the owning workstream, or the lead labels `lead-approved` |
| PR green but not merged | add the `automerge` label; check Actions has write permission |
| issue stuck `in-progress` after a crash | `gh issue edit <n> --remove-label in-progress --add-label ready` |
| a workstream seems paused | it has a `blocked` issue; unblock or close it |
