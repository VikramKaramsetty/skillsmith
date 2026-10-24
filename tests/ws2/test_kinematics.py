"""WS2.2: PlacoKinematics top-down IK and FK on the vendored SO101 URDF (skipped without placo or the URDF)."""

from __future__ import annotations

import math
from itertools import product

import pytest

from skillsmith.config import load_config

pytest.importorskip("placo")

CFG = load_config()
if not CFG.path(CFG.robot.urdf).is_file():
    pytest.skip("SO101 URDF not vendored yet: run python assets/so101/fetch_so101.py", allow_module_level=True)

from skillsmith.kinematics import MAX_POS_ERR, PlacoKinematics, make_kinematics  # noqa: E402

HOME_SEED = [0.0, 0.0, 0.0, 90.0, 0.0, 37.0]
ROUND_TRIP_TOL = 0.002  # m (WS2 acceptance)
# Workspace grid the demo uses (robot frame, metres): table level up to carry height.
# The tip is the grasp point, 3.5 cm past the fixed jaw (kinematics.TIP_OFFSET), so close to the base
# the arm cannot also be high: r <= 0.12 m at z >= 0.10 m is out of reach (test_near_base_high_is_out_of_reach).
NEAR_HIGH = [(0.12, 0.0, 0.10), (0.12, 0.0, 0.15)]
GRID = [p for p in product((0.12, 0.16, 0.20, 0.24), (-0.12, -0.06, 0.0, 0.06, 0.12), (0.0, 0.02, 0.05, 0.10, 0.15))
        if p not in NEAR_HIGH]


@pytest.fixture(scope="module")
def kin() -> PlacoKinematics:
    return make_kinematics(CFG)


def _dist(a, b) -> float:
    return math.dist(a, b)


def test_ik_fk_round_trip_over_workspace_grid(kin):
    for target in GRID:
        q = kin.ik(target, HOME_SEED)
        assert q is not None, f"no IK for {target}"
        assert len(q) == 6
        assert _dist(kin.fk(q), target) < ROUND_TRIP_TOL, target


def test_fk_ik_round_trip_from_joint_grid(kin):
    """Joint configs -> tip -> IK from a perturbed seed -> same tip."""
    checked = 0
    for pan, lift, elbow, wrist in product((-40.0, 0.0, 35.0), (-30.0, 0.0, 30.0), (-30.0, 0.0, 30.0), (60.0, 90.0)):
        q = [pan, lift, elbow, wrist, 0.0, 50.0]
        tip = kin.fk(q)
        if tip[2] < 0.0 or math.hypot(tip[0], tip[1]) > CFG.workspace.max_reach:
            continue
        seed = [v + 5.0 for v in q[:5]] + [50.0]
        sol = kin.ik(tip, seed)
        assert sol is not None, (q, tip)
        assert _dist(kin.fk(sol), tip) < ROUND_TRIP_TOL, (q, tip)
        checked += 1
    assert checked >= 20


def test_low_targets_point_straight_down(kin):
    """Grasp and place heights get a vertical gripper (the tip z-axis along world -z).
    Beyond r ~ 0.26 m even table-level targets need a little tilt (the wrist_flex limit)."""
    for x, y, z in GRID:
        if z <= 0.05 and math.hypot(x, y) <= 0.25:
            assert kin.ik((x, y, z), HOME_SEED) is not None
            assert kin.last_tilt_deg < 1.0, ((x, y, z), kin.last_tilt_deg)


def test_high_targets_tilt_only_as_needed(kin):
    """Above ~8 cm the SO101 wrist cannot point straight down; position stays exact, tilt is reported."""
    q = kin.ik((0.18, 0.0, 0.15), HOME_SEED)
    assert q is not None
    assert _dist(kin.fk(q), (0.18, 0.0, 0.15)) < 1e-3
    assert 0.0 < kin.last_tilt_deg < 45.0
    kin.max_tilt_deg = 5.0
    try:
        assert kin.ik((0.18, 0.0, 0.15), HOME_SEED) is None
        assert kin.ik((0.18, 0.0, 0.03), HOME_SEED) is not None
    finally:
        kin.max_tilt_deg = None


@pytest.mark.parametrize("target", [(0.60, 0.0, 0.05), (0.0, 0.0, 0.60), (0.35, 0.35, 0.30), (-0.50, 0.0, 0.0)])
def test_unreachable_targets_return_none(kin, target):
    assert kin.ik(target, HOME_SEED) is None


def test_gripper_copied_and_wrist_roll_held(kin):
    q = kin.ik((0.20, 0.05, 0.03), [0.0, 0.0, 0.0, 90.0, 12.0, 73.0])
    assert q is not None
    assert q[5] == 73.0
    assert q[4] == pytest.approx(12.0)


def test_solutions_respect_joint_limits(kin):
    for target in GRID:
        q = kin.ik(target, HOME_SEED)
        for name, deg in zip(kin.joint_names, q[:5]):
            lo, hi = kin._limits[name]
            assert lo - 0.1 <= deg <= hi + 0.1, (target, name, deg)


def test_vertical_waypoints_are_continuous(kin):
    """move_to splits vertical moves into 2 cm steps seeded from the current joints: no branch flips."""
    q = kin.ik((0.20, -0.05, 0.15), HOME_SEED)
    for k in range(1, 8):
        nxt = kin.ik((0.20, -0.05, 0.15 - 0.02 * k), q)
        assert nxt is not None
        assert max(abs(a - b) for a, b in zip(q[:5], nxt[:5])) < 20.0
        q = nxt


def test_fk_origin_is_under_the_pan_axis(kin):
    """Robot frame (AGENTS.md): origin on the table under shoulder_pan, so panning keeps the radius."""
    radii = [math.hypot(*kin.fk([pan, 10.0, 20.0, 60.0, 0.0, 0.0])[:2]) for pan in (-60.0, 0.0, 45.0)]
    assert max(radii) - min(radii) < 1e-6
    heights = [kin.fk([pan, 10.0, 20.0, 60.0, 0.0, 0.0])[2] for pan in (-60.0, 0.0, 45.0)]
    assert max(heights) - min(heights) < 1e-6


def test_fk_ignores_gripper_value(kin):
    assert kin.fk([10.0, 5.0, 5.0, 60.0, 0.0, 0.0]) == kin.fk([10.0, 5.0, 5.0, 60.0, 0.0, 100.0])


def test_near_base_high_is_out_of_reach(kin):
    """The price of a grasp-point tip; the raw fixed-jaw frame (tip_offset=0) reaches these."""
    raw = PlacoKinematics(str(CFG.path(CFG.robot.urdf)), CFG.robot.ee_frame, CFG.robot.joint_names[:5], tip_offset=0.0)
    for target in NEAR_HIGH:
        assert kin.ik(target, HOME_SEED) is None, target
        assert raw.ik(target, HOME_SEED) is not None, target


def test_tip_offset_moves_the_tip_along_the_jaws(kin):
    """Same joints, pointing down at pan 0: the grasp point is TIP_OFFSET further out, at the same height."""
    from skillsmith.kinematics import TIP_OFFSET

    raw = PlacoKinematics(str(CFG.path(CFG.robot.urdf)), CFG.robot.ee_frame, CFG.robot.joint_names[:5], tip_offset=0.0)
    q = raw.ik((0.18, 0.0, 0.03), HOME_SEED)
    assert raw.last_tilt_deg < 1.0
    (x0, y0, z0), (x1, y1, z1) = raw.fk(q), kin.fk(q)
    assert math.hypot(x1 - x0, y1 - y0) == pytest.approx(TIP_OFFSET, abs=5e-4)
    assert x1 - x0 > 0.03 and abs(z1 - z0) < 5e-4  # outward (the jaws open along +x here), not up or down


def test_residual_threshold_matches_contract():
    assert MAX_POS_ERR == 0.005


def test_rejects_wrong_joint_count():
    with pytest.raises(ValueError):
        PlacoKinematics(str(CFG.path(CFG.robot.urdf)), joint_names=["shoulder_pan"])
