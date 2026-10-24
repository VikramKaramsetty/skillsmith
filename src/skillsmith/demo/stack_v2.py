"""stack: put one object on top of another (reuses pick_place)."""

SKILL = {"name": "stack", "params": {"top": "str", "bottom": "str"}, "uses": ["pick_place"]}


def run(robot, scene, skills, top, bottom):
    a = scene.require(top)
    b = scene.require(bottom)
    robot.note(f"rim grip slipped last time; gripping {a.label} at mid-height via pick_place")
    skills.call("pick_place", obj=top, x=b.x, y=b.y, place_z=b.top + 0.002)
    robot.home()
