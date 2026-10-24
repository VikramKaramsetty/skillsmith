"""pyramid: stack objects into a stepped pyramid, widest at the bottom, narrowest on top.

Hand-written library skill (not Droid). It orders the objects by footprint and reuses the
saved `stack` skill for every level, so a fix Droid makes to `stack` (e.g. gripping cups at
mid-height) carries over here, just as it does for `tower`.
"""

SKILL = {"name": "pyramid", "params": {"objs": "list"}, "uses": ["stack"]}

MAX_LEVELS = 6


def footprint(o):
    return o.width


def run(robot, scene, skills, objs):
    if len(objs) < 2:
        raise SkillError(f"a pyramid needs at least 2 objects, got {len(objs)}")
    if len(objs) > MAX_LEVELS:
        raise SkillError(f"a pyramid of {len(objs)} is too tall; limit is {MAX_LEVELS}")
    levels = sorted([scene.require(name) for name in objs], key=footprint, reverse=True)
    for i in range(1, len(levels)):
        lower, upper = levels[i - 1], levels[i]
        if upper.width >= lower.width:
            raise SkillError(f"{upper.label} ({upper.width * 100:.1f} cm) is as wide as "
                             f"{lower.label} ({lower.width * 100:.1f} cm); each level must be "
                             f"narrower than the one below (use tower for equal sizes)")
    robot.note(f"building a pyramid of {len(levels)}: "
               f"{' / '.join(o.label for o in levels)} (widest first)")
    for i in range(1, len(levels)):
        robot.look()  # the next stack needs the height of the level just placed
        robot.note(f"level {i + 1} of {len(levels)}: {levels[i].label} on {levels[i - 1].label}")
        skills.call("stack", top=levels[i].id, bottom=levels[i - 1].id)
