# North Star: the single source of truth

> If any other document, brief, issue or comment disagrees with this one, **this one wins**.
> It changes only on a `lead/*` branch. Every agent reads it first.

## Objective

**A real SO-101 robot arm that gets better at physical tasks by itself.** When a task fails,
the system works out *what kind* of failure it was and *where* it happened. Factory Droid,
running inside the product through the **Droid Python SDK**, then gets the evidence: the failure
class, phase-by-phase checks, annotated camera frames and the numbers. It rewrites the skill
and proves the fix in simulation and safety checks. A human approves once, the real arm
succeeds, and the improved skill plus its lesson are kept for every future task.

The product is the **improvement loop**:

```
try → watch (live world model) → classify the failure → Droid repairs with evidence
    → prove (static, sim, safety) → human approves → succeed → remember (skill + lesson)
```

## Decision rule

Every task and PR must serve at least one of these, named in its hand-off ("Serves: 1, 3").
If it serves none, it goes on the cut list.

1. **Loop:** makes the repair loop on the real arm work, or work more reliably.
2. **Improvement:** makes the system better over time (fewer rounds, reusable skills and lessons).
3. **Control & visibility:** tightens what Droid can touch, or shows more of what Droid did.
4. **Safety:** required to run the real arm safely.

## The award: Best Use of the Factory Droid SDK

> **Eligibility:** The project must use the Droid SDK (TypeScript or Python) in a meaningful
> way. Droid should do real work in the running product, not just help you write code. The repo
> README should explain where and how Droid is used.
> **What scores well:** Droid runs on a trigger you designed, like an event, a schedule, a user
> action, or another agent. Thoughtful control over what Droid can touch, with safety built in.
> Making Droid's work visible: plans, steps, diffs, and results.
> **What scores poorly:** A chatbot wrapper with Droid added on. Demos that only work in slides.
> Projects where we can't tell what the agent did.

| Criterion | How we meet it | What a judge sees | Code |
|---|---|---|---|
| **Droid SDK doing real work in the product** | `DroidSDKAuthor` opens a `droid_sdk.Session` whenever a skill fails or a task is new. Without Droid the arm cannot learn a new task. | Droid writing code live while the arm waits | `repair/droid_sdk.py` |
| **README explains where and how** | "Where and how Droid is used" section | the repo | `README.md` |
| **Designed trigger** | The robot's own failure: the classifier labels a *skill* failure after a real attempt. Environment failures (camera bumped, object out of reach) go to a human, not Droid. Also: a gate rejects Droid's code, a human rejects with a note, a new task. | FAIL banner with the failure class, then Droid starting on its own | `repair/loop.py`, `analysis/` |
| **Thoughtful control, safety built in** | An **explicit tool allowlist**: Droid's only capabilities are ToolSearch/Script/TodoWrite and our `skillsmith` functions (read_file and write_skill are workspace-only; no file, shell, web, automation or sub-agent tools) · `make droid-sandbox-check` live-proves escapes are blocked (shell, fs, fetch, path traversal) · its fix is written by OUR code · we re-run static/sim/safety ourselves · the real arm is never a Droid tool · human approval · robotd joint clamps · live bump stop | The allowlist in the `droid.started` event; the sandbox check's output; every tool call | `repair/droid_sdk.py`, `repair/droid_tools.py`, `tools/droid_sandbox_check.py` |
| **Visible: plans, steps, diffs, results** | Failure class + phase, Droid's messages, each tool call (including its own sim runs), permission decisions, the diff, its structured diagnosis, gate results, real outcome | Dashboard big screen + detail drawer; `runs/<id>/events.jsonl` | `events.py`, `dashboard/` |
| *Avoid: chatbot wrapper* | No chat box; the robot's failure starts Droid. | | |
| *Avoid: works only in slides* | Live on the real arm; a recorded fallback is labelled as one. | | |
| *Avoid: can't tell what the agent did* | Every Droid action is an event; skill versions and lessons are kept in git. | | |

## What we are building, in one screen

- **Skills** are short Python programs over safe primitives (`move_to`, `grasp`, `release`,
  `look`, `home`, `align_over`). Droid writes and repairs skills, never motor commands.
- **Perception** is a **live world model**: a calibrated overhead camera, YOLO-World (HSV
  backup), ray-cast 3D positions, stable object ids, and a held object that follows the gripper.
  A bumped object stops the skill (`WorldChanged`). The wrist camera checks grasps and does the
  final alignment.
- **Failure analysis** is plain rules, not AI. It splits each attempt into phases (approach,
  grasp, lift, carry, place, release, retreat) and labels the failure class with numbers,
  sorting it into three buckets: **skill** (Droid fixes it), **environment** (a human or
  calibration fixes it), **unknown** (Droid gets everything, flagged). A wrong label is worse
  than "unknown", so low-confidence perception never produces a skill label.
- **Droid's evidence (v2)** leads with the failure class, then the phase table, annotated key
  frames (boxes, target crosshair, gripper position, error arrow in cm), cropped before/after
  images, expected-vs-actual frames, the trace, earlier rounds and lessons. Droid can pull more
  through tools and can test its fix in simulation before handing back.
- **Geometry decides pass/fail; vision only explains.** An optional frontier vision API gives a
  second opinion on cropped images; it never overrides geometry.
- **Small errors never reach Droid:** tolerance, closed-loop alignment (`align_over`) and
  calibration handle them. Droid is for strategy and logic errors.

Details: `docs/ARCHITECTURE.md` (how), `docs/INTERFACES.md` (contracts), `docs/PLAN.md`
(gates, schedule, time budget), `docs/DECISIONS.md` (why, sources, hard truths).

## Speed is a hard requirement

- **One repair round in under 60 s** on the demo task; about **1.5–2 minutes** for
  fail → fix → success (budget in `docs/PLAN.md`).
- Stage profile: `max_rounds 2`, `sim_tool_budget 1`, ~90 s per round, a fast model, and the
  session warmed up before the demo.
- Droid makes the smallest edit that fixes the stated failure, never a rewrite.
- The benchmark reports **one-round fix rate and seconds-to-fix per failure class**.

## Scope

- **Must (the demo):**
  - G3: the real arm reliably does a scripted pick-and-place, and the signals are validated
    (detection under 1 cm, gripper holding/empty separable, slip visible).
  - G5: "stack the blue cup on the red cup" with the block-tuned `stack` skill slipping on a
    cup, the classifier labelling it `SLIP_IN_CARRY`, one live Droid repair through the SDK in
    under 60 s, approval, real success, skill and lesson saved.
  - Dashboard big screen; live world model with bump stop; HSV detection as the floor.
- **Should:** YOLO-World as the main detector, wrist post-grasp check, `align_over` with wrist
  correction (if G3 shows over ~1 cm error), expected-vs-actual frames, Droid evidence tools, a
  second task reusing the saved skill ("build a tower"), benchmark numbers.
- **Stretch:** frontier-API vision second opinion, Droid-written monitors, self-calibration,
  pyramid.
- **Not this project:** VLA/pi05 policies driving the arm, Droid controlling motors, a chat UI,
  training models, millimetre precision, deformable or transparent objects.

## Rules that never bend

- Droid never moves the arm directly and never runs shell commands in the product.
- Every line Droid writes passes static → sim → safety → **human approval** before the real arm runs it.
- Environment-bucket failures are never sent to Droid as code problems.
- Geometry decides success; no vision model overrides it.
- Skills never hard-code offsets to cover position errors; that is calibration.
- The demo uses real Droid through the SDK. `ScriptedAuthor` is for tests; the CLI author is a
  stage-emergency fallback we would announce.
- If a document conflicts with this one, fix that document.
