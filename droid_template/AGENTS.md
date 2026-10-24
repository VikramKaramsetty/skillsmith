# AGENTS.md: rules for writing robot skills

You are writing or repairing one **skill**: a short Python program that moves a
SO-ARM101 robot arm by combining safe primitive moves. You never control motors directly.

## Your job

1. Read the evidence (the prompt, also saved as `failure.md`), then `API.md` (everything
   you may call), `LESSONS.md` (rules learned on earlier tasks: follow them), then `skill.py`.
2. The evidence leads with the **failure class** and the **phase** it happened in; trust it
   unless it says `UNKNOWN` or low confidence. Then read the **phase table**, the **expected vs
   actual** tables (where the success check wanted each object, and how the run that passed in
   simulation differed from this one), the **trace table** (every call, tip position
   before/after, the failing call marked **← failed here**) and the **scene before/after**
   tables. The camera snapshot and annotated key frames are attached as images (copies in
   `frames/`). `library/*.py` are working skills you can call or copy from.
   If that is not enough to be sure, pull more evidence (fast, no simulation budget):
   - `get_frame(phase)`: one phase's check, its calls and its annotated frame (saved into
     `frames/`; open it with Read). Phases: approach, grasp, lift, carry, place, release, retreat.
   - `get_object_track(name)`: where one object was through the attempt, e.g. when a held
     cup left the gripper.
   - `compare_with_success(phase)`: that phase next to the same phase of a run that passed,
     call by call, with how far apart the gripper tip was.
3. Make the **smallest change** to `skill.py` that fixes the stated failure. Never rewrite
   working parts. You are being timed live: aim to fix it in **one round**.
4. Edit **only `skill.py`**. Any other edit, and any shell command, is refused.
5. **Check your work with the `skillsmith` tools** before finishing:
   - `validate_skill`: the static checker. Fix everything it reports.
   - `simulate_skill`: runs your skill in simulation on a copy of the real table and returns
     PASS/FAIL with the trace. You have a few runs per round, so use them to iterate.
   - `get_scene`, `list_library`, `read_library_skill` when you need them.
   After you finish, the orchestrator re-runs every check itself and a human approves before
   the real arm moves. If anything fails you get new evidence in this same conversation.
6. Reply with the JSON result: `diagnosis` (what went wrong, with numbers), `change` (what
   you changed and why), `change_type` (`"parameter"` if you only changed numbers or
   constants, `"strategy"` if you changed the approach: which calls, their order, the logic),
   `sim_checked` (did `simulate_skill` pass on your final version), `confidence` (0–1), and
   `lesson` (one reusable rule for future skills, or "").
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

- No `import` (`math` is already available), `while`, `try`/`except`, `with`, `class`,
  `lambda`, `global`, `nonlocal`, `del`, generators or `async`.
- No attribute starting with `_`. No `eval exec compile open getattr setattr type …`.
- Use `robot`, `scene`, `skills` only as `robot.method(...)` or as a call argument.
  Don't assign them to other names or reassign them.
- Only the methods listed in `API.md`.
- `range()` with a literal bound of at most 50. At most 200 lines.

## Units and frame

Metres, seconds. Robot base frame: **x forward, y left, z up**, origin on the table
surface under the arm's base. `z = 0` is the table. Object `z` is the height of its
**base**; `top = z + height`. `robot.move_to(x, y, z)` places the **gripper tip**,
pointing straight down.

## Grasping heuristics that work on this arm

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
