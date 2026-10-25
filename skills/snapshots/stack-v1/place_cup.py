"""place_cup: carry the held cup to (x, y) and set its base down at height base_z (0 = the table), then let go.

Starts holding the cup the way pick_cup leaves it (side grasp, lifted 4.5 in). If it is not holding it yet,
it runs pick_cup first. base_z > 0 sets it down on something: for stacking, the top of the cup below.
"""

SKILL = {"name": "place_cup", "params": {"obj": "str", "x": "float", "y": "float", "base_z": "float",
                                        "offset": "tuple"},
         "uses": ["pick_cup"]}

MIN_CARRY_Z = 0.10    # m; never carry the tip lower than this
CLEAR_ABOVE = 0.03    # m; carried over a support (stacking), the cup's base stays this far above its top
SET_DOWN_GAP = 0.003  # m; stop this far above the target so the cup's base just meets its support
ARC_STEP_RAD = 0.15    # rad (~8.6 deg) of base rotation per carry waypoint
MAX_TIP_RADIUS = 0.325  # m; robot.max_reach is 0.33: pull the tip target in rather than have it refused


def run(robot, scene, skills, obj, x, y, base_z=0.0, offset=None):
    if not robot.holding():
        robot.note(f"not holding {obj} yet: picking it up first")
        offset = skills.call("pick_cup", obj=obj)
    cup = robot.look().require(obj)
    grip_below_top = 0.025           # pick_cup grips 2.5 cm under the cup's top (approach_side's default)
    grip_above_base = cup.height - grip_below_top
    hx, hy, hz = robot.position()
    # stay as high as pick_cup lifted it, and over a support keep the cup's base CLEAR_ABOVE its top
    carry_z = max(hz, MIN_CARRY_Z, base_z + grip_above_base + CLEAR_ABOVE)
    # the side grasp holds the cup with the fingertips past its centre: move the tip so the CUP lands on (x, y).
    # The offset comes from pick_cup (measured with the cup still on the table); a lifted cup reads off.
    ox, oy = offset if offset is not None else (hx - cup.x, hy - cup.y)
    tx, ty = x + ox, y + oy
    radius = math.hypot(tx, ty)
    if radius > MAX_TIP_RADIUS:  # the grip offset pushed it past the reach; the cup lands slightly short
        scale = MAX_TIP_RADIUS / radius
        tx, ty = tx / (1 / scale), ty / (1 / scale)
        robot.note(f"tip target pulled in to radius {MAX_TIP_RADIUS:.3f} (was {radius:.3f})")
    robot.note(f"carrying {cup.label} to ({x:.3f}, {y:.3f}) at tip z {carry_z:.3f}")
    robot.move_to(hx, hy, carry_z)
    # swing around the base instead of cutting across it: close to the base and high up the level gripper
    # cannot keep the tilt it gripped with (a straight carry from y +0.14 to -0.14 failed IK at radius 0.24)
    r0, r1 = math.hypot(hx, hy), math.hypot(tx, ty)
    a0, a1 = math.atan2(hy, hx), math.atan2(ty, tx)
    steps = max(1, math.ceil(abs(a1 - a0) / ARC_STEP_RAD))
    for k in range(1, steps + 1):
        f = k / steps
        r, a = r0 + (r1 - r0) * f, a0 + (a1 - a0) * f
        robot.move_to(math.cos(a) / (1 / r), math.sin(a) / (1 / r), carry_z)
    if not robot.holding():
        raise SkillError(f"lost {cup.label} while carrying it")
    robot.note(f"lowering {cup.label}: base to z={base_z:.3f}")
    robot.move_to(tx, ty, base_z + grip_above_base + SET_DOWN_GAP)
    robot.release()
    robot.note(f"released {cup.label}")
    robot.move_to(tx, ty, carry_z)
