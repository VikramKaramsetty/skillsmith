"""pick_cup: pick a cup up anywhere on the mat (approach_side picks side, straddle or top-down) and hold it up."""

SKILL = {"name": "pick_cup", "params": {"obj": "str"}, "uses": []}

TRIES = 3
TRAVEL_Z = 0.18
APPROACH_SPEED = 6


def run(robot, scene, skills, obj):
    robot.note(f"looking for {obj}")
    fresh = robot.look()
    cup = fresh.require(obj)
    held = False
    for attempt in range(TRIES):
        robot.note(f"side grasp on {cup.label}, try {attempt + 1}")
        robot.release()
        robot.approach_side(obj, speed=APPROACH_SPEED)
        if robot.grasp():
            held = True
            break
        px, py, pz = robot.position()
        robot.move_to(px, py, TRAVEL_Z)
        robot.home()
        cup = robot.look().require(obj)
    if not held:
        raise SkillError(f"side grasp missed {cup.label} {TRIES} times")
    hx, hy, held_z = robot.position()
    robot.note(f"lifting {cup.label}")
    robot.move_to(hx, hy, cup.top + 0.03)
    if not robot.holding():
        raise SkillError(f"lost {cup.label} while lifting it")
    robot.note(f"holding {cup.label} level, off the table")
