"""tower: build a tower by stacking objects one on another, bottom first.

Hand-written library skill (not Droid). It reuses the saved `stack` skill for every level,
so a fix Droid makes to `stack` (e.g. gripping cups at mid-height) carries over here.
"""

SKILL = {"name": "tower", "params": {"objs": "list"}, "uses": ["stack"]}

MAX_LEVELS = 6


def run(robot, scene, skills, objs):
    if len(objs) < 2:
        raise SkillError(f"a tower needs at least 2 objects, got {len(objs)}")
    if len(objs) > MAX_LEVELS:
        raise SkillError(f"a tower of {len(objs)} is too tall; limit is {MAX_LEVELS}")
    for name in objs:
        scene.require(name)
    robot.note(f"building a tower of {len(objs)}: {' / '.join(objs)} (bottom first)")
    for i in range(1, len(objs)):
        robot.look()  # the next stack needs the height of the level just placed
        robot.note(f"level {i + 1} of {len(objs)}: {objs[i]} on {objs[i - 1]}")
        skills.call("stack", top=objs[i], bottom=objs[i - 1])
