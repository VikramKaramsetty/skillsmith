"""stack: put one object on top of another."""

SKILL = {"name": "stack", "params": {"top": "str", "bottom": "str"}, "uses": []}

TRAVEL_Z = 0.18


def run(robot, scene, skills, top, bottom):
    a = scene.require(top)
    b = scene.require(bottom)
    robot.note(f"stacking {a.label} on {b.label}")
    grip_z = a.top - 0.01  # grab near the rim
    robot.release()
    robot.move_to(a.x, a.y, TRAVEL_Z)
    robot.move_to(a.x, a.y, grip_z)
    if not robot.grasp():
        raise SkillError(f"missed {a.label}")
    robot.move_to(a.x, a.y, TRAVEL_Z)
    robot.move_to(b.x, b.y, TRAVEL_Z)
    if not robot.holding():
        raise SkillError(f"lost {a.label} while carrying it")
    robot.move_to(b.x, b.y, b.top + (grip_z - a.z) + 0.005)
    robot.release()
    robot.move_to(b.x, b.y, TRAVEL_Z)
    robot.home()
