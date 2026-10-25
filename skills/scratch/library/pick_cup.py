"""pick_cup: pick a cup up anywhere on the mat (approach_side picks side, straddle or top-down) and hold it up."""

SKILL = {"name": "pick_cup", "params": {"obj": "str"}, "uses": []}

TRIES = 3
TRAVEL_Z = 0.18
APPROACH_SPEED = 6
LIFT = 0.1143      # m (4.5 in): the held cup's base clears another standing cup (9.5 cm tall) by ~2 cm


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
    on_table = robot.look().require(obj)  # still on the table: the camera reads it right (lifted it reads off)
    offset = (hx - on_table.x, hy - on_table.y)  # where the tip holds the cup, relative to the cup's centre
    robot.note(f"lifting {cup.label} {LIFT / 0.0254:.1f} in")
    robot.move_to(hx, hy, held_z + LIFT)
    if not robot.holding():
        raise SkillError(f"lost {cup.label} while lifting it")
    robot.note(f"holding {cup.label} level, off the table (tip {offset[0] / 0.01:+.1f}, {offset[1] / 0.01:+.1f} cm "
               "off its centre)")
    return offset
