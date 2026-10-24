"""WS7.3: a repeated xy offset is fitted from test places and corrected by OffsetKinematics.

The fake world is perfectly calibrated, so `Miscalibrated` plays a kinematic model that is
off by a fixed shift: commanding p puts the tip at p + offset while fk still reports p.
"""

import random

import pytest

from skillsmith.calibration import (
    OffsetKinematics,
    apply_offsets,
    fit_offset,
    load_offsets,
    offset_spread,
    repeated_offset,
    save_offsets,
)
from skillsmith.config import REPO_ROOT, load_config
from skillsmith.events import NullBus
from skillsmith.hal.base import Env
from skillsmith.hal.fake import FakeWorld
from skillsmith.skills.runtime import run_skill
from skillsmith.types import Scene, SceneObject

cfg = load_config()
PICK_PLACE = (REPO_ROOT / "skills/library/pick_place.py").read_text()
OFFSET = (-0.004, 0.012)  # every place lands 4 mm back and 1.2 cm left
TARGETS = [(0.22, 0.10), (0.25, -0.05), (0.18, -0.12), (0.28, 0.02)]


class Miscalibrated:
    def __init__(self, inner, dx, dy):
        self.inner, self.dx, self.dy = inner, dx, dy

    def fk(self, joints):
        x, y, z = self.inner.fk(joints)
        return (x - self.dx, y - self.dy, z)

    def ik(self, target, seed):
        return self.inner.ik((target[0] + self.dx, target[1] + self.dy, target[2]), seed)


def place_at(x, y, wrap=None):
    """Pick a block and place it at (x, y); return where the camera sees it end up."""
    block = SceneObject("red_block_0", "red block", "red", "block", 0.15, 0.0, 0.0, 0.03, 0.04)
    w = FakeWorld.from_scene(Scene((block,), 0.0, "fake"), cfg)
    kin = Miscalibrated(w, *OFFSET)
    env = Env("fake", w, wrap(kin) if wrap else kin, w)
    r = run_skill(PICK_PLACE, env, {"obj": "red block", "x": x, "y": y, "place_z": 0.0}, cfg,
                  NullBus(), skill="pick_place")
    assert r.ok, r.reason()
    b = w.observe().find("red block")
    return (b.x, b.y)


def test_fit_recovers_synthetic_offset_within_2mm():
    rng = random.Random(7)
    commanded = [(rng.uniform(0.12, 0.3), rng.uniform(-0.15, 0.15), 0.0) for _ in range(8)]
    measured = [(x + 0.015 + rng.gauss(0, 0.001), y - 0.006 + rng.gauss(0, 0.001), z)
                for x, y, z in commanded]
    dx, dy = fit_offset(commanded, measured)
    assert abs(dx - 0.015) < 0.002 and abs(dy + 0.006) < 0.002
    assert repeated_offset(commanded, measured) == (dx, dy)


def test_scattered_residuals_are_not_a_repeated_offset():
    commanded = [(0.2, 0.0), (0.2, 0.1), (0.25, -0.1), (0.3, 0.0)]
    measured = [(0.22, 0.0), (0.18, 0.1), (0.25, -0.08), (0.3, -0.02)]
    assert offset_spread(commanded, measured) > 0.005
    assert repeated_offset(commanded, measured) is None
    assert repeated_offset(commanded[:2], [(0.21, 0.0), (0.21, 0.1)]) is None  # too few points


def test_fit_rejects_bad_input():
    with pytest.raises(ValueError):
        fit_offset([], [])
    with pytest.raises(ValueError):
        fit_offset([(0, 0)], [(0, 0), (1, 1)])


def test_offset_kinematics_shifts_ik_and_leaves_fk():
    w = FakeWorld.demo(cfg)
    k = OffsetKinematics(w, 0.01, -0.02)
    q = k.ik((0.2, 0.0, 0.05), w.joints())
    assert q[:3] == pytest.approx([0.19, 0.02, 0.05])
    assert k.fk(q) == w.fk(q)


def test_fit_from_fake_places_then_correct():
    """End to end on the fake world: miss, fit, wrap, land within 2 mm."""
    measured = [place_at(x, y) for x, y in TARGETS]
    assert all(abs(m[1] - t[1] - OFFSET[1]) < 1e-6 for m, t in zip(measured, TARGETS))
    fit = repeated_offset(TARGETS, measured)
    assert fit is not None
    assert fit == pytest.approx(OFFSET, abs=0.002)

    for x, y in TARGETS:
        mx, my = place_at(x, y, wrap=lambda kin: OffsetKinematics(kin, *fit))
        assert abs(mx - x) < 0.002 and abs(my - y) < 0.002


def test_offsets_file_round_trip(tmp_path):
    path = tmp_path / "calibration" / "offsets.json"
    w = FakeWorld.demo(cfg)
    assert load_offsets(path) is None
    assert apply_offsets(w, path) is w
    save_offsets(0.004, -0.002, path)
    assert load_offsets(path) == (0.004, -0.002)
    k = apply_offsets(w, path)
    assert isinstance(k, OffsetKinematics) and (k.dx, k.dy) == (0.004, -0.002)
    path.write_text("{not json")
    with pytest.raises(ValueError):
        load_offsets(path)
