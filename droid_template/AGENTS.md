# AGENTS.md: rules for writing robot skills

You are writing or repairing one **skill**: a short Python program that moves a
SO-ARM101 robot arm by combining safe primitive moves. You never control motors directly.

## Your tools

You have **only** these, all from the `skillsmith` tool server (call them from a Script as
`tools.skillsmith.<name>({...})`). There are no other file, shell or web tools.

| Tool | Use |
|---|---|
| `read_file(name)` | Text files in your workspace: `failure.md`, `skill.py`, `LESSONS.md`, `AGENTS.md`, `API.md`, `scene.json`, `library/<skill>.py`. It refuses images. |
| `write_skill(source)` | Save the **complete** new `skill.py` (whole file, not a diff). The only way to change the skill; the static checker runs at once and lists any issue. |
| `validate_skill()` | The static checker on your current `skill.py`. |
| `simulate_skill()` | Runs your `skill.py` in simulation on a copy of the real table: PASS/FAIL, trace, safety report. A few runs per round. |
| `get_scene()`, `list_library()`, `read_library_skill(name)` | The current objects; saved skills you can call or copy from. |
| `get_frame(phase)` | One phase's check, its calls and what its annotated frame shows, as text: objects with positions, gripper tip, planned target, error arrow in cm. Phases: approach, grasp, lift, carry, place, release, retreat, crop_before, crop_after. Empty = the failing phase. |
| `get_object_track(name)` | Where one object was through the attempt, e.g. when a held cup left the gripper. |
| `compare_with_success(phase)` | That phase next to the same phase of a run that passed, call by call, with how far apart the gripper tip was. |

Your reply also carries `skill_source`: the complete new `skill.py`. The orchestrator saves
it and re-runs every check, so it must match what you last saved with `write_skill`.

## Your job

1. The evidence is in the prompt (also `failure.md`). The rules (this file) and the skill API
   (`API.md`) are in your instructions. Get `skill.py` and `LESSONS.md` (rules learned on
   earlier tasks: follow them) with `read_file`.
2. The evidence leads with the **failure class** and the **phase** it happened in; trust it
   unless it says `UNKNOWN` or low confidence. Then study the **phase table**, the **expected vs
   actual** tables (where the success check wanted each object, and how the run that passed in
   simulation differed from this one), the **trace table** (every call, tip position
   before/after, the failing call marked **← failed here**) and the **scene before/after**
   tables. The camera snapshot and annotated key frames are attached to the prompt as images.
   If that is not enough to be sure, pull more evidence with `get_frame`, `get_object_track`
   or `compare_with_success` (fast, no simulation budget).
3. Make the **smallest change** to `skill.py` that fixes the stated failure. Never rewrite
   working parts. You are being timed live: aim to fix it in **one round**.
4. Change the skill **only with `write_skill`**. Nothing else in the workspace can be changed,
   and no command can be run.
5. **Check your work** before finishing: fix everything `write_skill` / `validate_skill`
   report, then `simulate_skill` until it passes (or the budget runs out).
   After you finish, the orchestrator re-runs every check itself and a human approves before
   the real arm moves. If anything fails you get new evidence in this same conversation.
6. Reply with the JSON result: `diagnosis` (what went wrong, with numbers), `change` (what
   you changed and why), `change_type` (`"parameter"` if you only changed numbers or
   constants, `"strategy"` if you changed the approach: which calls, their order, the logic),
   `sim_checked` (did `simulate_skill` pass on your final version), `confidence` (0–1),
   `lesson` (one reusable rule for future skills, or "") and `skill_source` (the complete
   new `skill.py`).
   If the evidence says a value-only change already failed for this class, your next change
   must be a `"strategy"` change.

## No magic offsets

Never add a constant to compensate for a position error ("+0.012 in x because it lands
1.2 cm short"). A repeatable offset between where the camera sees an object and where the
gripper goes is **calibration**, fixed outside skills; a skill that hides it breaks on every
other object and table position. Compute every target from scene positions and object sizes
(`obj.x`, `obj.y`, `obj.z`, `obj.height`, `support.top`). If the evidence shows the gripper
consistently missing by the same amount in every phase, say so in `diagnosis` instead of
patching it in code.

## Skill file format (exact)

```python
"""stack: put one object on top of another."""

SKILL = {"name": "stack", "params": {"top": "str", "bottom": "str"}, "uses": ["pick_place"]}

TRAVEL_Z = 0.18          # constants are fine


def helper(robot, x, y):  # helper functions are fine; pass robot in
    robot.move_to(x, y, TRAVEL_Z)


def run(robot, scene, skills, top, bottom):
    ...
```

- First three parameters of `run` are exactly `robot, scene, skills`; then the task params.
- Top level holds only the docstring, constants (`NAME = ...`), and functions.
- List every library skill you call in `SKILL["uses"]`.

## Static rules (code breaking these is rejected before it runs)

`write_skill` and `validate_skill` run this checker; the orchestrator runs it again.

- No `import` (`math` is already available), `while`, `try`/`except`, `with`, `class`,
  `global`, `nonlocal`, `async`, `yield` (no generators) or `lambda`. No decorators.
- No attribute starting with `_`. None of the names `eval exec compile open __import__ getattr
  setattr delattr globals locals vars input breakpoint type object super`.
- Only the names the runtime gives you (the builtins listed in `API.md`, `math`, `SkillError`,
  `ValueError`, `RuntimeError`) and names your skill binds itself. No `print`: use `robot.note`.
- Only the `robot.*`, `scene.*`, `skills.*` attributes listed in `API.md`. No frame or
  generator introspection attributes, no `.format` (use f-strings), no `.mro`.
- Use `robot`, `scene`, `skills` only as `robot.attr` or as a call argument: no aliasing,
  no reassigning. Pass them to your own helper functions only as parameters of the same name
  (`helper(robot, x, y)` with `def helper(robot, x, y)`).
- No attribute assignment (`obj.x = ...`) and no mutating robot or scene data.
- `range()` bound at most 50 (it must fold to a constant). No recursion.
- Exponents must be constants with |e| ≤ 10; no `<<`; sequence repetition (`[0] * n`) at
  most 1000.
- Top-level assignments are constants only. At most 200 lines.
- A `def run(robot, scene, skills, ...)` must exist.

## Units and frame

Metres, seconds. Robot base frame: **x forward, y left, z up**, origin on the table
surface under the arm's base. `z = 0` is the table. Object `z` is the height of its
**base**; `top = z + height`. `robot.move_to(x, y, z)` places the **gripper tip**,
pointing straight down, or LEVEL (parallel to the table) after `robot.approach_side`.

## Grasping heuristics that work on this arm

**Cups: grasp from the side, gripper level.** The demo cup is upside down (closed top), tapered
(7.6 cm on the table, 5.1 cm at the top), 9.5 cm tall. Top-down grips miss it. Use:

```python
robot.approach_side(obj)        # reach in from the side at 1.5 cm under its top, jaws open
if not robot.grasp():
    raise SkillError("side grasp missed")
held_z = robot.position()[2]
robot.move_to(robot.position()[0], robot.position()[1], cup.top + 0.03)   # stays level
robot.move_to(x, y, cup.top + 0.03)
robot.move_to(x, y, held_z + 0.003)   # set it down at the height it was picked at
robot.release()
robot.move_to(x, y, cup.top + 0.03)
robot.home()                          # home points down again
```

- A side grasp reaches cups about **0.22–0.33 m from the base** (it reaches in from ~4 cm short of
  the cup, so it needs room behind it). Nearer is `Unreachable` before moving: report it, don't tune
  heights. Keep level moves at ≥ 0.18 m.
- While holding a cup level, travel at `cup.top + 0.03`, not 0.18 (too high to stay level).

Blocks and other small objects (top-down):

- `robot.release()` first so the gripper is open before descending.
- Travel between places at `TRAVEL_Z = 0.18`, high enough to clear a held cup over
  another cup.
- Grip at **mid-height**: `obj.z + obj.height * 0.5`. Rim grips slip on the real arm.
- Descend and ascend vertically (same x, y). Vertical moves are split into 2 cm steps for you.
- After `grasp()`, check its return value; after carrying, check `robot.holding()`.
- To place on something: lower until the held object's base is ~5 mm above the support's
  `top`, i.e. tip z = `support.top + grip_offset + 0.005`. Then `release()`, then lift
  back to `TRAVEL_Z`.
- End every skill with `robot.home()`.
- The workspace is roughly x 0.08–0.34, y −0.20–0.20, z 0.005–0.25, reach ≤ 0.33 m.
  Targets outside it are refused before the arm moves.

## Style

- **Reuse** library skills with `skills.call("pick_place", obj=..., x=..., y=..., place_z=...)`
  instead of re-writing them.
- **Narrate** with `robot.note("lowering blue cup onto red cup")` before each phase: notes
  appear live on the dashboard judges watch.
- **Fail loudly**: if something is missing or a check fails, `raise SkillError("specific
  reason with numbers")`. Never hide a failure.
- Use `scene.require("blue cup")` (raises a clear error) instead of `scene.find` unless the
  object is optional. Call `robot.look()` for a fresh scene after moving things.
