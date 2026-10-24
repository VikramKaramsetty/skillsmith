"""stack: put one object on top of another.

Hand-written seed skill (not Droid), tuned for blocks: it pinches the top GRIP_DEPTH of the
object, which on a 3 cm block is most of its height. On a tall cup that is the rim, and the
cup slips out as soon as the arm carries it sideways. That is the demo's honest failure.
"""

SKILL = {"name": "stack", "params": {"top": "str", "bottom": "str"}, "uses": []}

TRAVEL_Z = 0.18
GRIP_DEPTH = 0.012  # how far below the top the jaws close; fine for blocks
PLACE_CLEARANCE = 0.005


def run(robot, scene, skills, top, bottom):
    a = scene.require(top)
    b = scene.require(bottom)
    grip_z = a.top - GRIP_DEPTH
    robot.note(f"stacking {a.label} on {b.label}, gripping {GRIP_DEPTH * 100:.1f} cm below its top")
    robot.release()
    robot.move_to(a.x, a.y, TRAVEL_Z)
    robot.align_over(top)
    x, y, _ = robot.position()
    robot.move_to(x, y, grip_z)
    if not robot.grasp():
        raise SkillError(f"grasp missed {a.label} at z={grip_z:.3f}")
    robot.move_to(x, y, TRAVEL_Z)
    robot.move_to(b.x, b.y, TRAVEL_Z)
    if not robot.holding():
        raise SkillError(f"lost {a.label} while carrying it (gripped at z={grip_z:.3f}, top {a.top:.3f})")
    robot.align_over(bottom)
    x, y, _ = robot.position()
    robot.move_to(x, y, b.top + (grip_z - a.z) + PLACE_CLEARANCE)
    robot.release()
    robot.move_to(x, y, TRAVEL_Z)
    robot.home()
