"""sort_by_color: put every object of one kind into the container of the same colour.

Hand-written library skill (not Droid). Reuses `put_in` for each object. It checks that every
object has a matching container before moving anything, so it never half-sorts and then fails.
"""

SKILL = {"name": "sort_by_color", "params": {"kind": "str", "into": "str"}, "uses": ["put_in"]}


def run(robot, scene, skills, kind="block", into="cup"):
    items = [o for o in scene.objects if o.kind == kind]
    bins = [o for o in scene.objects if o.kind == into]
    if not items:
        raise SkillError(f"no {kind}s to sort (saw: {', '.join(o.label for o in scene.objects)})")
    plan = []
    for o in items:
        match = [b for b in bins if b.color == o.color]
        if not match:
            have = ", ".join(b.label for b in bins) or "none"
            raise SkillError(f"no {o.color} {into} for {o.label} (have: {have})")
        plan.append((o, match[0]))
    robot.note(f"sorting {len(plan)} {kind}s into {into}s by colour")
    for o, b in plan:
        robot.look()  # positions may have shifted since the last drop
        skills.call("put_in", obj=o.id, container=b.id)
    robot.home()
