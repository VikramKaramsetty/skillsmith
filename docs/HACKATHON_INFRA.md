# Hackathon infrastructure: decisions and why

Each decision trades a little elegance for not getting stuck at 3 a.m.

## Team and process

**Contract first, fake backend first.** Interfaces and a pure-Python fake world were
written before any feature work, so five agents (and humans on other machines) can build
in parallel with no hardware, no MuJoCo, and no Droid account. `make demo-fake` runs the
whole loop in under a second, which makes it the regression test for every merge.

**One owner per file, one worktree per agent, CI-enforced.** Parallel agents fail when
they edit the same files. `.github/ownership.yml` maps each branch prefix to its paths
and CI rejects PRs that stray; shared contracts change only on `lead/*` branches. See
`ORCHESTRATION.md`.

**GitHub as the hub: issue queue + self-merging PRs.** Every machine runs
`scripts/agent-loop.sh all`. It claims the next free task (one in flight per workstream,
races settled by comment order), codes in a worktree, and opens a PR. Green CI (`check`,
`heavy`, `ownership`) triggers the `automerge` job. No human merge step except for
contract changes. See `docs/MULTI_MACHINE.md`.

**Hardware is a mutex held by a human.** Agents never move the real arm. They hand off
"needs hardware test" items, and the arm operator runs them in a batch.

## Robot software

**A single `robotd` owns the follower serial port and the cameras.** Only one process can
hold a serial port or a camera at a time, and teleop, tests and streams kept colliding.
Everything else is an HTTP client. robotd is also the last safety layer (joint limits,
per-step clamp).

**The event log is the single source of truth for the UI.** Every step emits an event to
`runs/<id>/events.jsonl`. The dashboard renders only from events, so a recorded run
replays as the demo fallback, and post-mortems need no extra logging.

**Robot model:** MuJoCo Menagerie's
[`robotstudio_so101`](https://github.com/google-deepmind/mujoco_menagerie/tree/main/robotstudio_so101)
for sim (derived from TheRobotStudio's `so101_new_calib`, with added collision
primitives for the gripper), plus TheRobotStudio's `so101_new_calib.urdf` for IK. Its
zero is mid-range per joint, which should match LeRobot's calibrated degrees. **Verify
signs at G3.** Menagerie's actuator gains differ from upstream, so tune for settling, not
for realism.

**IK: placo directly, the same way LeRobot does it.** LeRobot's
[`RobotKinematics`](https://github.com/huggingface/lerobot/blob/main/src/lerobot/model/kinematics.py)
is a thin placo wrapper (joints in degrees, frame `gripper_frame_link`, position weight
1.0, small orientation weight). We call placo ourselves in `kinematics.py` so the sim, CI
and every machine avoid LeRobot's torch dependency, and one class serves both sim and
real. **Do not** apply the hqfang pi05 joint convention (signs `[1,-1,1,1,1,1]`, offsets
`[0,90,90,0,0,0]`); that is only for the VLA checkpoint.

**Table positions come from an ArUco homography.** Objects rest on the table plane, so
pixel → (x, y) is a 3×3 homography from four markers at known robot-frame points. No
depth camera and no stereo are needed, and it is about 30 minutes of work.

**HSV colour detection first; an open-vocabulary detector on the GX10 as the stretch.**
Coloured cups and blocks are what the demo uses. The GPU model is a cut-list item.

**A fixed overhead webcam beats the MacBook stand-in.** The Mac stream crosses a
Tailscale relay (125–235 ms) and the laptop moves. A $20 USB webcam on the GX10, taped
above the table, gives stable homography and lower latency.

## Droid

**The Droid Python SDK, not the CLI.** The award requires the
[Droid SDK](https://docs.factory.ai/sdk/python) doing real work in the running product, and
the SDK gives us the controls the criteria reward:
- `SessionConfig(autonomy=LOW, disabled_tools=["Execute"])`: no shell.
- **SDK MCP tools** (`validate_skill`, `simulate_skill`, ...): Droid tests its own fix in sim
  through *our* code instead of running commands.
- an `on_permission` handler: our policy decides every edit and tool call, in code.
- a **JSON-schema result** (diagnosis, change, confidence, lesson) for the dashboard and the lessons file.
- one long-lived `Session` per run: memory across repair rounds.
- typed stream events (`ToolCall`, `ToolResult`, `AssistantMessage`), so every step is visible.

Static checks, sim and safety still run in our orchestrator after Droid returns, so Droid
cannot skip or weaken them. `droid exec` stays only as a stage-emergency fallback
(`droid.mode: cli`). Verified against `droid-sdk` 0.5.0.

**The workspace sits outside the repo.** Droid reads `AGENTS.md` from its working
directory. Putting the runtime workspace in `~/.skillsmith/droid_ws/<run>` keeps the
build-time `AGENTS.md` out of its context.

**Repair between attempts, not mid-motion.** Recent work shows multi-turn code repair is
too slow for real-time control ([RHO](https://www.emergentmind.com/papers/2606.16458)),
so the arm stops, Droid repairs, gates re-run, and then the arm tries again.

## Networking and demo resilience

**USB-Ethernet link-local between the Mac and the GX10 for the demo.** The GX10's public
Wi-Fi drops and Tailscale relays add 125–235 ms. The GX10's `Wired connection 3` is
already link-local; plug the adapter into the Mac side too. Droid still needs internet:
if the GX10 loses Wi-Fi, run Droid on the Mac against the synced workspace (see
`RUNBOOK_GX10.md`).

**Fallback ladder.** Live real → live sim only → replay of a recorded `events.jsonl` plus
a screen recording. Record the fallback before you need it (gate G6).

**Visualisation:** a custom FastAPI + SSE page, because it needs an Approve button and
the narrative timeline. [Rerun](https://rerun.io) is a good optional debug viewer for
engineers (camera + joint states + timeline scrubbing); pin its version if used.

## Hard truths

Things that will bite if we pretend otherwise. Each has a mitigation; none can be wished away.

1. **URDF ↔ calibration mismatch is the #1 hardware risk.** If joint signs or zeros differ
   between `so101_new_calib` and LeRobot's calibrated degrees, every IK target is wrong
   and the arm drives somewhere unexpected. *Do G3 (WS2.7) in the first hours, at
   `max_tcp_speed: 0.05`, hand on the power.*
2. **The SO101 is a hobby arm.** Expect backlash and ~5–10 mm repeatability from
   STS3215 servos. *Pick cup-sized tasks with ≥ 1 cm tolerance; never demo precision.*
3. **The sim gate checks geometry, not grasping.** The weld-on-grasp trick means MuJoCo
   cannot predict slips, tipping or squeezed cups. A skill can pass sim and fail on the
   real arm. *That is why the camera verify exists, and that gap is the demo's story; say it plainly.*
4. **Droid is slow on stage.** One repair round (prompt → edits → static → sim → safety)
   is tens of seconds to a few minutes. *Narrate it with the dashboard's Droid panel,
   measure latency in WS5, and pick the fastest model that still fixes the benchmark bugs.*
5. **Droid's fix rate is unknown until WS5.3 measures it.** *Tune `droid_template/` and
   the failure report against the seeded-bug benchmark. Never show `ScriptedAuthor` and
   call it Droid; it exists for tests and rehearsal only.*
6. **HSV detection depends on the lighting, and the venue's lighting is not ours.**
   *Re-tune thresholds on site (WS3.4 tool), tape the camera down (moving it breaks the
   homography), and use matte, saturated objects.*
7. **`exec` with stripped builtins is not a sandbox.** It works here only because the
   validator, the guard, sim, human approval and robotd don't depend on it. Say so if asked.
8. **`demo-fake` proves plumbing, not robotics.** Its physics is a toy. Real confidence
   starts at G2 (sim) and G5 (full loop on the real arm).
9. **The network is hostile.** The GX10's public Wi-Fi drops, Tailscale adds 125–235 ms,
   and Droid needs the internet. *USB-Ethernet for Mac↔GX10, Droid on the Mac as a
   fallback, and a recorded `events.jsonl` + video for the worst case.*
10. **The GX10 has crashed before** (NVIDIA "bad register read"). *Watch `dmesg`; keep
    the sim and dashboard runnable on a laptop.*
11. **One arm is a single point of failure.** A stripped gear or a cooked servo ends the
    demo. *The leader arm has the same 6 STS3215 motors, so it is the spare-parts kit;
    know how to swap one, and re-ID it, before you need to.*
12. **Agents can write code that passes tests and is still wrong.** Ownership prevents
    merge conflicts, not bad design, and cross-workstream bugs only show at QA and G5.
    *Keep acceptance tests behavioural, check `make status` hourly, and integrate on real
    hardware early, not at hour 20.*
13. **Untested so far:** a full headless `claude -p` agent run through `agent-run.sh`,
    Droid on the GX10 (aarch64), and every hardware step. *The first agent-loop run is
    the test; watch it.*
14. **Scope will not all fit.** The real MVP is one task (stack cups) with one live Droid
    repair on the real arm. Vision model, calibration skill and pyramid are the cut list
    (`BUILD_PLAN.md`), so cut early, not at midnight.
