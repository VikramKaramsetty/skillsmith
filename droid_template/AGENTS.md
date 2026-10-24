# AGENTS.md: rules for writing robot skills

You are writing or repairing one **skill**: a short Python program that moves a
SO-ARM101 robot arm by combining safe primitive moves. You never control motors directly.

## Your job

1. Read the evidence (the prompt, also saved as `failure.md`), then `API.md` (everything
   you may call), `LESSONS.md` (rules learned on earlier tasks: follow them), then `skill.py`.
2. Look at the **trace table** (every call, tip position before/after, errors, the failing
   line) and the **scene before/after** tables. The camera snapshot is attached as an image.
   `library/*.py` are working skills you can call or copy from.
3. Make the **smallest change** to `skill.py` that fixes the stated failure. Don't rewrite
   working parts.
4. Edit **only `skill.py`**. Any other edit, and any shell command, is refused.
5. **Check your work with the `skillsmith` tools** before finishing:
   - `validate_skill`: the static checker. Fix everything it reports.
   - `simulate_skill`: runs your skill in simulation on a copy of the real table and returns
     PASS/FAIL with the trace. You have a few runs per round, so use them to iterate.
   - `get_scene`, `list_library`, `read_library_skill` when you need them.
   After you finish, the orchestrator re-runs every check itself and a human approves before
   the real arm moves. If anything fails you get new evidence in this same conversation.
6. Reply with the JSON result: `diagnosis` (what went wrong, with numbers), `change` (what
   you changed and why), `sim_checked` (did `simulate_skill` pass on your final version),
   `confidence` (0–1), and `lesson` (one reusable rule for future skills, or "").

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
