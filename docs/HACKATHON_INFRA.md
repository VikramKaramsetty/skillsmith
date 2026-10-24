# Hackathon infrastructure: decisions and why

Each decision trades a little elegance for not getting stuck at 3 a.m.

## Team and process

**Contract first, fake backend first.** Interfaces and a pure-Python fake world were
written before any feature work, so five agents (and humans on other machines) can build
in parallel with no hardware, no MuJoCo, and no Droid account. `make demo-fake` runs the
whole loop in under a second, which makes it the regression test for every merge.

**One owner per file, one worktree per agent, lead-only merges.** Parallel agents fail
when they edit the same files. Ownership is written into each workstream file; shared
files belong to the lead. See `ORCHESTRATION.md`.

**GitHub PR pipeline across machines.** Agents on other laptops and accounts push
branches and open PRs; CI runs `make check`; the lead merges in dependency order. See
`docs/MULTI_MACHINE.md`.

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

**IK:** LeRobot's [`RobotKinematics`](https://github.com/huggingface/lerobot/blob/main/src/lerobot/model/kinematics.py)
(a placo wrapper over Pinocchio): `inverse_kinematics(current_joint_pos, desired_ee_pose,
position_weight=1.0, orientation_weight=0.01)`, joints in degrees, frame
`gripper_frame_link`. It's already in the GX10's LeRobot venv, so there's no new
dependency. **Do not** apply the hqfang pi05 joint convention (signs `[1,-1,1,1,1,1]`,
offsets `[0,90,90,0,0,0]`); that is only for the VLA checkpoint.

**Table positions come from an ArUco homography.** Objects rest on the table plane, so
pixel → (x, y) is a 3×3 homography from four markers at known robot-frame points. No
depth camera and no stereo are needed, and it is about 30 minutes of work.

**HSV colour detection first; an open-vocabulary detector on the GX10 as the stretch.**
Coloured cups and blocks are what the demo uses. The GPU model is a cut-list item.

**A fixed overhead webcam beats the MacBook stand-in.** The Mac stream crosses a
Tailscale relay (125–235 ms) and the laptop moves. A $20 USB webcam on the GX10, taped
above the table, gives stable homography and lower latency.

## Droid

**Headless `droid exec`, low autonomy, no command execution.**
[Droid exec](https://docs.factory.ai/cli/droid-exec/overview) is read-only by default;
`--auto low` allows file edits only, and `--remove-tools Execute` removes command
execution. Static checks, sim and safety run in our orchestrator after Droid returns, so
Droid cannot skip or weaken them. `-o stream-json` streams each tool call to the
dashboard; `--session-id` keeps memory across repair rounds. Exit 0 doesn't mean
success, so we always verify ourselves.

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
