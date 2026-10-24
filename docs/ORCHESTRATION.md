# Orchestration: building Skillsmith with parallel agents

The lead's playbook: which agents run, in what order, and how their work comes back
together. The mechanics of running agents across several machines (issue queue,
auto-merging PRs) are in `docs/MULTI_MACHINE.md`.

## Principles

1. **Contract first.** `types.py`, `hal/base.py`, `primitives.py`, `repair/protocols.py`,
   `events.py` and `docs/INTERFACES.md` already exist. Agents build behind them and
   never wait on each other.
2. **Fake first.** The `fake` backend and `ScriptedAuthor` already run the whole loop
   (`make demo-fake`). Every workstream can test end to end without hardware, MuJoCo or Droid.
3. **One owner per file**, enforced in CI (`.github/ownership.yml`).
4. **One worktree per agent**, one machine per workstream.
5. **Gates, not vibes.** Done means `make check` is green in CI and the brief's
   acceptance tests pass. A human runs the hardware acceptance tests.
6. **Hardware is a mutex.** One arm operator runs every `needs-hardware` issue.

## Workstreams

| WS | Name | Brief | Depends on |
|---|---|---|---|
| WS1 | robotd + real arm client | `workstreams/WS1_robotd_real.md` | contracts |
| WS2 | kinematics + MuJoCo sim | `workstreams/WS2_kinematics_sim.md` | contracts |
| WS3 | perception | `workstreams/WS3_perception.md` | contracts (WS2 for marker touch-off) |
| WS4 | runtime + safety hardening | `workstreams/WS4_runtime_safety.md` | contracts |
| WS5 | Droid repair loop | `workstreams/WS5_repair_droid.md` | contracts (WS4 soft) |
| WS6 | dashboard | `workstreams/WS6_dashboard.md` | event schema only |
| WS7 | calibration + library (stretch) | `workstreams/WS7_calibration_library.md` | WS1–3 |
| QA | integration tests | `workstreams/QA_integration.md` | whatever has merged |

## Waves

```
Wave 0 (done)       contracts, fake world, validator, runtime, loop, demo-fake   G0 G1
Wave 1 (parallel)   WS1  WS2  WS3  WS4  WS5.1  WS6        + human: G3 first hardware test
Wave 2 (parallel)   WS5 (real Droid)  WS2.6 (G2)  QA  WS3/WS1 hardware issues      G2 G4
Wave 3              integration on the real arm (human-led), demo tasks, WS7      G5
Wave 4              freeze, rehearse ×3, record the fallback video                G6
```

Across machines, waves happen on their own: each loop works its queue in order.
Within one machine, run at most ~5 agents at once; past that, review is the bottleneck.

## Running a wave inside one Claude Code session

Send all of the wave's agents in **one message**, each with `isolation: "worktree"` and
the matching `subagent_type` from `.claude/agents/`:

```
subagent_type: ws2-kinematics-sim
isolation: worktree
prompt: Do task "WS2.1 Vendor the SO101 URDF ..." from docs/workstreams/WS2_kinematics_sim.md.
        Stop when make check passes; write HANDOFF.md.
```

Then ship each worktree with `scripts/ship.sh "ws2: ..." <issue>`.

## Running it across machines

See `docs/MULTI_MACHINE.md`. In short: `scripts/setup-github.sh` once, then
`scripts/agent-loop.sh <workstreams>` on each machine. CI merges green PRs.

## Factory Droid as the builder (good for the Droid-track story)

Any loop can use Droid instead of Claude: `AGENT=droid scripts/agent-loop.sh ws4`.
`agent-run.sh` calls `droid exec --auto medium` (edits, builds and tests, but no pushes),
and the runner does the pushing. Saying that Droid both builds the system and repairs
the robot's skills at runtime is a strong point for the judges.

## Contract changes

1. An agent puts a `CONTRACT CHANGE REQUEST` in its `HANDOFF.md` (the PR body).
2. The lead makes the change on a `lead/<slug>` branch: code + `docs/INTERFACES.md` +
   the affected briefs, labelled `contract-change`, merged by hand.
3. Loops pick it up automatically: every task starts from the latest `main`.

## When an agent gets stuck

- Issue labelled `blocked` → read the PR and the log in `../skillsmith-logs/`, sharpen
  the issue text, relabel it `ready` (or take the task yourself).
- Needs hardware facts → `docs/RUNBOOK_GX10.md`; if the answer is missing, add it there.
- Two workstreams need the same file → the lead splits the file or makes the change.
