# Decisions, hard truths and sources

Why the system looks the way it does. Each decision says what we chose, why, and what we
gave up. The objective is in `docs/NORTH_STAR.md`; the mechanics are in `docs/ARCHITECTURE.md`.

## Decisions

**D1. Code as policies, not a VLA.** Droid writes short skills over tested primitives; no neural
net sits in the motor loop. Repairs are readable diffs, safety checks are possible, and the
award wants visible agent work. *Gave up:* dexterity on unstructured objects.

**D2. The Droid Python SDK, not the CLI.** The award requires the SDK doing real work. It gives
autonomy levels, disabled tools, a permission callback, custom tools (`simulate_skill`),
JSON-schema results, persistent sessions and typed stream events. Verified against droid-sdk
0.5.0. `droid exec` remains a stage-emergency fallback. **Python over the TypeScript SDK**
(`@factory/droid-sdk`, also award-eligible): our orchestrator and tools are Python, so the
Python SDK calls `simulate_skill` and the permission policy in-process; the TypeScript SDK would
need a separate Node service plus a bridge to those tools. Both read `FACTORY_API_KEY` (created at
app.factory.ai/settings/api-keys, kept in the shell profile, never in the repo).

**D3. Classify before Droid sees anything.** Plain rules label the failure class, phase, numbers
and bucket. Droid starts in the right place (needed for one round under 60 s), the dashboard
gets a readable banner, and the benchmark can report results per class. Environment failures
never reach Droid. A wrong label is worse than UNKNOWN, so rules fire only on unambiguous
evidence and low perception confidence forces an environment or unknown class.

**D4. Evidence v2: step-level and visually grounded.** Models diagnose well from per-step,
image-grounded evidence and badly from a single end-of-task verdict (REFLECT, Inner Monologue,
AHA, ViFailback). So: a phase table, annotated key frames, cropped before/after, expected vs
actual, tools to fetch more.

**D5. Geometry decides, vision explains.** Vision judges of success are unreliable, especially
on contact-heavy steps (FailBench, Guardian), and miss failed grasps. Predicates on positions
are the authority; an optional frontier vision API gives an advisory second opinion on crops.

**D6. A live world model.** A one-shot scene goes stale the moment the arm bumps something. The
world model tracks at 10–15 Hz, keeps ids, lets the held object follow the gripper (Rational
Inverse Reasoning's heuristic), keeps hidden objects at their last position, and stops the skill
on an unexpected move (`WorldChanged`).

**D7. 3D positions by ray casting, not a homography.** A homography is exact only on the table
plane. An overhead camera sees a cup's top: parallax ≈ object height × off-centre distance /
camera height, e.g. 8 cm × 20 cm / 80 cm = **2 cm** (about 4 cm for a stacked cup). A calibrated
camera (checkerboard + ArUco pose) and a ray–plane intersection at the object's known height
fix this for about the same work. No depth camera: unneeded for known objects, and new hardware.

**D8. Small errors never reach Droid.** Tolerance, closed-loop `align_over` (wrist camera) and
calibration handle centimetre errors. Droid handles strategy and logic. Skills never hard-code
offsets.

**D9. One process owns the hardware.** Only robotd opens the serial port and cameras (one process
per device), and it is the last safety layer (joint limits, per-step clamp, watchdog).

**D10. The event log is the UI's only input.** Every step is an event; the dashboard renders
only from events, so a recorded run replays as the demo fallback.

**D11. Contract-first, fake-first, CI-gated parallel work.** Shared contracts plus a pure-Python
fake let many agents on many machines build at once. Ownership checks and self-merging PRs keep
it coherent (`docs/WORKFLOW.md`).

**D12. Repair between attempts, not mid-motion.** Multi-turn code repair is too slow for
real-time control (RHO). The arm stops, Droid repairs, the gates re-run, the arm retries.
Within an attempt, closed-loop alignment and step checks handle small corrections.

**D13. Networking for the demo.** USB-Ethernet link-local between the Mac and the GX10 (the
public Wi-Fi drops; Tailscale adds 125–235 ms). Droid needs internet; if the GX10 loses it, run
the orchestrator on the Mac (robotd with `--host` + token).

## Hard truths

1. **URDF ↔ calibration mismatch is the #1 hardware risk.** Verify joint signs and offsets
   first, slowly, hand on the power (WS2.7).
2. **The SO-101 is a hobby arm**: backlash, ~5–10 mm repeatability. Cup-sized tasks, ≥ 1 cm tolerance.
3. **Perception decides what we can know.** Every position, and most failure classes, come from
   camera images. Bad lighting or a bumped camera produces confident wrong numbers; hence the
   confidence gate. Re-tune on site, tape the camera down.
4. **Gripper "holding" is unproven** on this arm with our cups. A soft cup may squash flat and
   read as empty. Test it (WS1.7) before trusting `SLIP_IN_CARRY`; the wrist check is the backup.
5. **Identical cups nest**: they don't stack. Use a smaller cup on an upturned cup, or a block.
6. **The sim checks geometry, not grasping.** Weld-on-grasp means MuJoCo can't predict slips.
   That gap is why the camera check exists, and it is the demo's story.
7. **Droid's fix rate and latency are unknown until WS5.6 measures them.** Never present
   `ScriptedAuthor` as Droid.
8. **Some failures aren't visible to any sensor we have** (a cup tilting in the gripper, a
   squeezed cup). They are UNKNOWN; Droid reasons from the images; recurring ones become rules
   that *we* write.
9. **`exec` with stripped builtins is not a sandbox.** It is fine only because the validator, the
   guard, the sim, human approval and robotd don't depend on it.
10. **`demo-fake` proves plumbing, not robotics.** Confidence starts at G2 and G3.
11. **The network is hostile and the GX10 has crashed before** (NVIDIA "bad register read").
    Keep the fallback recording; watch `dmesg`.
12. **One arm is a single point of failure.** The leader arm's six STS3215 motors are the spare kit.
13. **Agents can write code that passes tests and is still wrong.** Behavioural acceptance tests,
    hourly `make status`, integrate on hardware early.
14. **Scope will not all fit.** Cut from the top of the cut list in `docs/PLAN.md`, early.

## Open questions (answer at G3)

- Overhead camera: which one, how high, how centred?
- Is the wrist camera mounted and giving usable frames?
- Jaw opening vs cup diameter: does a side grip fit, or must we grip the rim?
- Real placement error open-loop: is wrist alignment needed?

## Sources

| Source | What we took |
|---|---|
| [Droid Python SDK](https://docs.factory.ai/sdk/python), [droid exec](https://docs.factory.ai/cli/droid-exec/overview) | D2: sessions, autonomy, permission handler, SDK MCP tools, JSON results |
| [REFLECT](https://arxiv.org/abs/2306.15724) (CoRL 2023) | D4: hierarchical summary, first failed subgoal → phase table |
| [Inner Monologue](https://proceedings.mlr.press/v205/huang23c.html) (CoRL 2022) | D4: per-step success feedback |
| [AHA](https://arxiv.org/abs/2410.00371) (ICLR 2025) | D4/D5: subtask + key frame as input; VLM failure reasoning is useful but imperfect |
| [FailBench](https://arxiv.org/pdf/2609.03611), [Guardian](https://arxiv.org/pdf/2512.01946v1) | D5: vision judges ~0.77 balanced accuracy, worse on contact; cropping helps |
| [ViFailback](https://arxiv.org/abs/2512.02787) (CVPR 2026) | D4: draw symbols (arrows, crosshairs) on frames |
| [KitchenVLA](https://www.merl.com/publications/docs/TR2025-068.pdf) (MERL 2025) | D4: compare with reference key frames |
| [Code-as-Monitor](https://arxiv.org/abs/2412.04455) | stretch: Droid-written live monitors |
| [Rational Inverse Reasoning](https://arxiv.org/pdf/2508.08983), [SAM2 tracking](https://arxiv.org/pdf/2607.17323), [POGS](https://autolab.berkeley.edu/assets/publications/media/2025-ICRA-POGS-CRv5.pdf) | D6: held object follows the gripper; tracking rates |
| [HyCodePolicy](https://arxiv.org/abs/2508.02629), [RoboInspector](https://arxiv.org/abs/2508.21378) | D1/D4: sim execution + feedback-driven code repair |
| [RHO](https://www.emergentmind.com/papers/2606.16458) | D12: multi-turn repair too slow mid-motion |
| [MuJoCo Menagerie robotstudio_so101](https://github.com/google-deepmind/mujoco_menagerie/tree/main/robotstudio_so101), [LeRobot kinematics](https://github.com/huggingface/lerobot/blob/main/src/lerobot/model/kinematics.py) | robot model; placo IK the way LeRobot does it |

Figures quoted from papers come from abstracts and search summaries; check the paper before
quoting a number to judges.
