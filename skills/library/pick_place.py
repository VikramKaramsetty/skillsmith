"""pick_place: pick an object up and set it down at (x, y) with its base at place_z.

Hand-written seed skill (not Droid). Grips at mid-height, travels high, lowers in steps.
"""

SKILL = {
    "name": "pick_place",
    "params": {"obj": "str", "x": "float", "y": "float", "place_z": "float"},
    "uses": [],
}

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
