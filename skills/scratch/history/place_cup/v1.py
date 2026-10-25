"""place_cup: carry the held cup to (x, y) and set it down there, then let go.

Starts holding the cup the way pick_cup leaves it (side grasp, gripper level). If it is not holding it
yet, it runs pick_cup first.
"""

SKILL = {"name": "place_cup", "params": {"obj": "str", "x": "float", "y": "float"}, "uses": ["pick_cup"]}

CARRY_Z = 0.10      # m; tip height while carrying ("up to about 10 cm")
SET_DOWN_GAP = 0.003  # m; stop this far above the grip height so the cup base just meets the table


def run(robot, scene, skills, obj, x, y):
    if not robot.holding():
        robot.note(f"not holding {obj} yet: picking it up first")
        skills.call("pick_cup", obj=obj)
    cup = scene.require(obj)
    grip_z = cup.top - 0.025  # pick_cup grips 2.5 cm under the top (approach_side's default)
    hx, hy, _ = robot.position()
    robot.note(f"raising {cup.label} to {CARRY_Z * 100:.0f} cm")
    robot.move_to(hx, hy, CARRY_Z)
    robot.note(f"carrying {cup.label} to ({x:.3f}, {y:.3f})")
    robot.move_to(x, y, CARRY_Z)
    if not robot.holding():
        raise SkillError(f"lost {cup.label} while carrying it")
    robot.note(f"lowering {cup.label} onto ({x:.3f}, {y:.3f})")
    robot.move_to(x, y, grip_z + SET_DOWN_GAP)
    robot.release()
    robot.note(f"released {cup.label}")
    robot.move_to(x, y, CARRY_Z)
