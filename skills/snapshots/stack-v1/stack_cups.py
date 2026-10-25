"""stack_cups: put one cup on T-point (x, y), then the other cup on top of it.

The cups are the demo's paper cups, upside down. The first is the cup nearest (x, y): it stays where it
is, untouched. The second is lifted 4.5 in (clears the first), carried over it, lowered until its base is just above
the first cup's top, and released: it drops over the first cup and nests.
"""

SKILL = {"name": "stack_cups", "params": {"x": "float", "y": "float"}, "uses": ["pick_cup", "place_cup"]}

DROP_GAP = 0.005  # m; release the top cup with its base this far above the bottom cup's top


def run(robot, scene, skills, x, y):
    cups = [o for o in robot.look().objects if o.kind == "cup"]
    if len(cups) < 2:
        raise SkillError(f"need two cups on the table, saw {len(cups)}")
    near = sorted((math.hypot(o.x - x, o.y - y), k) for k, o in enumerate(cups))
    bottom, top = cups[near[0][1]], cups[near[1][1]]
    # the cup nearest (x, y) is already set there: never touch it, stack the other one on it where it stands
    robot.note(f"bottom: {bottom.id} stays at ({bottom.x:.3f}, {bottom.y:.3f}); stacking {top.id} on it")
    offset = skills.call("pick_cup", obj=top.id)
    skills.call("place_cup", obj=top.id, x=bottom.x, y=bottom.y, base_z=bottom.top + DROP_GAP, offset=offset)
    robot.home()
