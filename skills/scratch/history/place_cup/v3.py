"""place_cup: carry the held cup to (x, y) and set its base down at height base_z (0 = the table), then let go.

Starts holding the cup the way pick_cup leaves it (side grasp, lifted 4.5 in). If it is not holding it yet,
it runs pick_cup first. base_z > 0 sets it down on something: for stacking, the top of the cup below.
"""

SKILL = {"name": "place_cup", "params": {"obj": "str", "x": "float", "y": "float", "base_z": "float"},
         "uses": ["pick_cup"]}

MIN_CARRY_Z = 0.10    # m; never carry the tip lower than this
SET_DOWN_GAP = 0.003  # m; stop this far above the target so the cup's base just meets its support


def run(robot, scene, skills, obj, x, y, base_z=0.0):
    if not robot.holding():
        robot.note(f"not holding {obj} yet: picking it up first")
        skills.call("pick_cup", obj=obj)
    cup = robot.look().require(obj)  # where the held cup is NOW (the scene passed in is from before the pick)
    grip_below_top = 0.025           # pick_cup grips 2.5 cm under the cup's top (approach_side's default)
    grip_above_base = cup.height - grip_below_top
    hx, hy, hz = robot.position()
    carry_z = max(hz, MIN_CARRY_Z)   # stay as high as pick_cup lifted it
    # the side grasp holds the cup with the fingertips past its centre: move the tip so the CUP lands on (x, y)
    ox, oy = hx - cup.x, hy - cup.y
    tx, ty = x + ox, y + oy
    robot.note(f"carrying {cup.label} to ({x:.3f}, {y:.3f}) at tip z {carry_z:.3f}")
    robot.move_to(hx, hy, carry_z)
    robot.move_to(tx, ty, carry_z)
    if not robot.holding():
        raise SkillError(f"lost {cup.label} while carrying it")
    robot.note(f"lowering {cup.label}: base to z={base_z:.3f}")
    robot.move_to(tx, ty, base_z + grip_above_base + SET_DOWN_GAP)
    robot.release()
    robot.note(f"released {cup.label}")
    robot.move_to(tx, ty, carry_z)
