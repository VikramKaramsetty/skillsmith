"""put_in: pick an object up and drop it into an open container (cup, bowl, bin).

Hand-written library skill (not Droid). Grips at mid-height, aligns over the container and
lets go just above its rim, so the object falls in without the jaws touching the walls.
"""

SKILL = {"name": "put_in", "params": {"obj": "str", "container": "str"}, "uses": []}

TRAVEL_Z = 0.18
RIM_CLEARANCE = 0.01  # object base this far above the rim when the jaws open


def run(robot, scene, skills, obj, container):
    a = scene.require(obj)
    c = scene.require(container)
    if a.width >= c.width:
        raise SkillError(f"{a.label} ({a.width * 100:.1f} cm) is too wide for {c.label} "
                         f"({c.width * 100:.1f} cm opening)")
    grip_z = a.z + a.height * 0.5
    robot.note(f"putting {a.label} in {c.label}")
    robot.release()
    robot.move_to(a.x, a.y, TRAVEL_Z)
    robot.align_over(obj)
    x, y, _ = robot.position()
    robot.move_to(x, y, grip_z)
    if not robot.grasp():
        raise SkillError(f"grasp missed {a.label} at z={grip_z:.3f}")
    robot.move_to(x, y, TRAVEL_Z)
    robot.move_to(c.x, c.y, TRAVEL_Z)
    if not robot.holding():
        raise SkillError(f"lost {a.label} while carrying it to {c.label}")
    robot.align_over(container)
    x, y, _ = robot.position()
    robot.move_to(x, y, c.top + (grip_z - a.z) + RIM_CLEARANCE)
    robot.release()
    robot.move_to(x, y, TRAVEL_Z)
