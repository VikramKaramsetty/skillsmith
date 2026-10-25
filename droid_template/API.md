# Skill API

Everything a skill can use. Anything else is rejected by the static check.

## `robot`

| Call | Meaning |
|---|---|
| `robot.move_to(x, y, z, speed=None, side=None)` | Gripper tip to (x, y, z) metres. `side=None` keeps the current orientation (level after `approach_side`, else pointing down); `True` = level, `False` = pointing down. Long vertical moves are split into ≤ 2 cm steps. `speed` in m/s, capped by config. Refused (SafetyViolation) if outside the workspace; Unreachable if IK fails (level: nearer than about 0.18 m from the base). |
| `robot.grasp() -> bool` | Close the gripper. True if something is held. |
| `robot.release()` | Open the gripper fully. |
| `robot.look() -> Scene` | Fresh object detection. |
| `robot.home()` | Go to the safe home pose. |
| `robot.position() -> (x, y, z)` | Current tip position (read-only). |
| `robot.holding() -> bool` | Gripper load check (read-only). |
| `robot.note(text)` | Show a short message on the dashboard. |
| `robot.align_over(obj, hover=0.08) -> float` | Hover `hover` m above the object's top at its **latest** position, then fine-align with the wrist camera if available. Returns the remaining xy error in metres. Use it right before descending to grasp or place. |
| `robot.approach_side(obj, grip_z=None, hover=0.03) -> float` | **Side grasp (use for cups), like a hand.** Opens the jaws, turns the gripper level (jaws opening sideways; fingers may point up to 30° down near the base) high up behind the object, fixes its position with the wrist camera, lowers to `grip_z` (default 1.5 cm under its top) a few cm short of it, slides in horizontally until the jaws are either side of it, and closes the gap to the fixed jaw. Call `grasp()` next; later `move_to` calls stay level. Returns the wrist-camera correction (m). Unreachable nearer than about 0.22 m from the base (it reaches in from ~4 cm short of the cup). |

Every call is recorded in the trace you see in `failure.md`.

If something on the table gets bumped while the skill runs, the skill stops with
`WorldChanged` before the next move. Don't try to catch it; the failure report explains it.

## `scene`

| Call | Meaning |
|---|---|
| `scene.find(q) -> object or None` | Match by id, then exact label, then "color kind", then label containing `q`. |
| `scene.require(q) -> object` | Like `find`, but raises `SkillError("'q' not found in scene (saw: …)")`. |
| `scene.objects` | Tuple of all objects. |

Object fields: `.id` ("blue_cup_0"), `.label` ("blue cup"), `.color`, `.kind` ("cup",
"block", …), `.x .y` (centre of the base), `.z` (height of the base above the table),
`.height`, `.width` (diameter or edge), `.top` (= z + height), `.confidence`.

## `skills`

`skills.call(name, **params)` runs a saved library skill with the same robot and scene.
See `library/` for what exists. Maximum nesting depth is 3.

## Also available (no import)

`math` (e.g. `math.hypot`, `math.atan2`), the builtins `abs min max round len range
float int str bool list dict tuple enumerate zip sorted sum any all`, and `SkillError`,
`ValueError`, `RuntimeError` to raise.

## Example: pick and place

```python
"""pick_place: pick an object up and set it down at (x, y) with its base at place_z."""

SKILL = {"name": "pick_place", "params": {"obj": "str", "x": "float", "y": "float", "place_z": "float"}, "uses": []}

TRAVEL_Z = 0.18


def run(robot, scene, skills, obj, x, y, place_z=0.0):
    target = scene.require(obj)
    grip_offset = target.height * 0.5
    robot.note(f"picking {target.label}")
    robot.release()
    robot.move_to(target.x, target.y, TRAVEL_Z)
    robot.move_to(target.x, target.y, target.z + grip_offset)
    if not robot.grasp():
        raise SkillError(f"grasp missed {target.label} at z={target.z + grip_offset:.3f}")
    robot.move_to(target.x, target.y, TRAVEL_Z)
    robot.move_to(x, y, TRAVEL_Z)
    if not robot.holding():
        raise SkillError(f"lost {target.label} while carrying it")
    robot.move_to(x, y, place_z + grip_offset + 0.005)
    robot.release()
    robot.move_to(x, y, TRAVEL_Z)
```

## Example: move a cup (side grasp)

```python
"""move_cup: pick a cup up from the side and set it down at (x, y)."""

SKILL = {"name": "move_cup", "params": {"obj": "str", "x": "float", "y": "float"}, "uses": []}


def run(robot, scene, skills, obj, x, y):
    cup = scene.require(obj)
    carry_z = cup.top + 0.03
    robot.note(f"side grasp on {cup.label}")
    robot.approach_side(obj)
    if not robot.grasp():
        raise SkillError(f"side grasp missed {cup.label}")
    hx, hy, held_z = robot.position()
    robot.move_to(hx, hy, carry_z)
    robot.move_to(x, y, carry_z)
    if not robot.holding():
        raise SkillError(f"lost {cup.label} while carrying it")
    robot.move_to(x, y, held_z + 0.003)
    robot.release()
    robot.move_to(x, y, carry_z)
    robot.home()
```

## Example: stack (reuses pick_place)

```python
"""stack: put one object on top of another."""

SKILL = {"name": "stack", "params": {"top": "str", "bottom": "str"}, "uses": ["pick_place"]}


def run(robot, scene, skills, top, bottom):
    a = scene.require(top)
    b = scene.require(bottom)
    robot.note(f"stacking {a.label} on {b.label}")
    skills.call("pick_place", obj=top, x=b.x, y=b.y, place_z=b.top + 0.002)
    robot.home()
```
