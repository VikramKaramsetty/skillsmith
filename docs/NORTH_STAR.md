# North Star: the single source of truth

> If any other document, brief, issue or comment disagrees with this one, **this one wins**.
> It changes only on a `lead/*` branch. Every agent reads it before `AGENTS.md`.

## Objective

**A real SO-101 robot arm that gets better at physical tasks by itself.** When a task fails,
Factory Droid, running inside the product through the **Droid Python SDK**, diagnoses the
failure from camera and trace evidence and rewrites the skill. It proves the fix in simulation
and safety checks, a human approves once, the real arm succeeds, and the improved skill and the
lesson behind it are kept for every future task.

The product is the **improvement loop**: try → camera verifies → Droid repairs → prove →
approve → succeed → remember. Everything in this repo exists to make that loop run live,
safely and visibly.

## The decision rule

Every task, PR and feature must serve at least one of these. Name it in the hand-off note
("Serves: 2"). If it serves none, it goes on the cut list.

1. **Loop:** makes Droid's repair loop on the real arm work, or work more reliably.
2. **Improvement:** makes the system better over time (better skills, fewer rounds, lessons reused).
3. **Control & visibility:** tightens what Droid can touch, or shows more of what Droid did.
4. **Safety:** required for running the real arm safely.

## The award we are built for: Best Use of the Factory Droid SDK

The criteria, verbatim:

> **Eligibility:** The project must use the Droid SDK (TypeScript or Python) in a meaningful
> way. Droid should do real work in the running product, not just help you write code. The repo
> README should explain where and how Droid is used.
> **What scores well:** Droid runs on a trigger you designed, like an event, a schedule, a user
> action, or another agent. Thoughtful control over what Droid can touch, with safety built in.
> Making Droid's work visible: plans, steps, diffs, and results.
> **What scores poorly:** A chatbot wrapper with Droid added on. Demos that only work in slides.
> Projects where we can't tell what the agent did.

How each one is met (keep this table true; it is our judging checklist):

| Criterion | How Skillsmith meets it | What a judge sees | Code |
|---|---|---|---|
| **Uses the Droid SDK, real work in the running product** | `DroidSDKAuthor` opens a `droid_sdk.Session` at runtime for every failed or new task. Without Droid the arm cannot learn a new task. | Droid writing code live while the arm waits | `repair/droid_sdk.py` |
| **README explains where and how** | "Where Droid runs" section in `README.md` | the repo | `README.md` |
| **Trigger you designed** | The robot's own failure: the camera's success check fails after a real attempt. Also: a gate rejects Droid's code, a human rejects with a note, a never-seen task arrives, grasps drift (calibration). | The FAIL banner, then Droid starting on its own | `repair/loop.py` |
| **Thoughtful control, safety built in** | Autonomy LOW · shell execution disabled · permission handler: edits allowed **only** to `skill.py` in Droid's workspace, tool calls **only** to our `skillsmith` tools, everything else refused with a reason · workspace outside the repo · we re-run static/sim/safety ourselves · the real arm is never a Droid tool · human approval · robotd joint clamps | Every permission decision listed live (allowed/refused + why) | `repair/droid_tools.py` (`decide`), `docs/SAFETY.md` |
| **Visible: plans, steps, diffs, results** | Droid's messages (plan), each tool call and result (steps), every permission decision, the `skill.py` diff, its structured result (diagnosis, change, confidence, lesson), gate results, sim video, real outcome | Dashboard timeline + Droid panel; `runs/<id>/events.jsonl` | `events.py`, dashboard (WS6) |
| *Avoid: chatbot wrapper* | No chat box. Droid is started by the robot failing, not by someone typing to it. | | |
| *Avoid: works only in slides* | Live on the real arm. The recorded fallback is labelled as a recording. | | |
| *Avoid: can't tell what the agent did* | Every Droid action is an event; skill versions + lessons are kept in git. | | |

## How Droid gets feedback and improves the skill

```
 real attempt ──▶ camera check FAILS ──▶ evidence package ──▶ Droid (SDK session)
                                                               │  reads evidence
                                                               │  edits skill.py
                                                               │  calls validate_skill / simulate_skill
                                                               │  returns structured result
                                                               ▼
 real success ◀── human approves ◀── safety ◀── sim ◀── static   (WE re-run every gate)
      │                                  any gate fails → new evidence, same session
      ▼
 save skill vN+1 · append lesson · count success
```

**1. The evidence package** (one per round, in `failure.md` plus attachments):
- the task, its parameters and the exact success check (e.g. `on_top(blue cup, red cup)`)
- one line on why it failed, plus the camera check's numbers ("blue cup is 9.4 cm from red cup, limit 3.0 cm")
- the safety report
- scene before and after: every object's x, y, z, top and width
- the full trace: every primitive call with its arguments, gripper tip position before and
  after, success, and any error with the exact skill line that raised it
- the camera snapshot, attached as an image
- earlier rounds in this run ("do not repeat these") and any human reviewer note
- `LESSONS.md`: everything Droid has learned on earlier tasks
- the working library skills it can call or copy

**2. Droid's tools**, our code in our sandbox; Droid cannot run shell commands:

| Tool | What it does |
|---|---|
| `validate_skill` | runs the static safety checker on the current `skill.py` |
| `simulate_skill` | runs `skill.py` in simulation on a copy of the real table; returns success, trace and safety (budget of a few runs per round) |
| `get_scene` | the current object table |
| `list_library` / `read_library_skill` | saved skills to reuse via `skills.call(...)` |

So Droid can fix, test in sim, and fix again **before** handing back: "Droid fixes its own code until it works".

**3. What Droid must return:** the edited `skill.py` and a structured result
`{diagnosis, change, sim_checked, confidence, lesson}`, enforced by a JSON schema via the SDK.

**4. What we do with it:** diff it, then re-run static → sim → safety ourselves (Droid's own sim
run is never trusted). A human approves, the real arm attempts, the camera verifies. If anything
fails, a new evidence package goes into **the same session**, so Droid remembers what it tried.

**5. On success:** the skill is saved as a new version (old versions kept), Droid's lesson is
appended to `skills/lessons.md`, and the skill's success count goes up.

## Constant improvement, at four time scales

| Scale | Mechanism | Metric we report |
|---|---|---|
| Within one repair | Droid iterates with `simulate_skill` | sim runs per repair |
| Within one task | repair rounds in one SDK session with full history | rounds to fix |
| Across tasks | versioned skill library reused via `skills.call` + `lessons.md` in every prompt | first-try success on new tasks, reuse count, lessons used |
| Across hardware drift | calibration trigger when grasps keep landing off (WS5.6/WS7) | grasp error over time |

The dashboard's improvement panel (WS6.7) shows these numbers.

## Scope

- **Must (the demo):** G3 and G5 with "stack the blue cup on the red cup": a real failure, one
  live Droid repair through the SDK, the dashboard showing Droid's steps and permission decisions,
  approval, real success, skill saved.
- **Should:** a second task that reuses the saved skill ("build a tower"); a measured fix rate
  from the seeded-bug benchmark (WS5.3); a lesson from task 1 visibly used in task 2.
- **Stretch:** self-calibration, open-vocabulary vision on the GX10, pyramid.
- **Not this project:** VLA/pi05 policies driving the arm, Droid controlling motors, a chat UI,
  training models, millimetre-precision tasks.

## Rules that never bend

- Droid never moves the arm directly and never runs shell commands in the product.
- Every line Droid writes passes static → sim → safety → **human approval** before the real arm runs it.
- The demo uses real Droid through the SDK. `ScriptedAuthor` is for tests only. The `droid exec`
  CLI author is a fallback for a stage emergency, and we say so if we use it.
- If a document conflicts with this one, fix that document.
