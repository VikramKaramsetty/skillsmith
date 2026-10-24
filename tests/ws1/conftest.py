"""Shared fixtures for WS1 (robotd) tests. robotd lives at the repo root, outside src/."""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from robotd.controller import ArmController  # noqa: E402
from robotd.mock import MockBus  # noqa: E402


class StuckElbowBus(MockBus):
    """A mock arm whose elbow_flex servo never moves, like a stalled joint under load."""

    def write_goal(self, goal):
        elbow = self.present[2]
        super().write_goal(goal)
        self.present[2] = elbow


def goals(bus: MockBus, joint: int = 0) -> list[float]:
    """Every goal value written to one joint, in order."""
    return [g[joint] for kind, g in bus.log if kind == "goal"]


@pytest.fixture
def bus() -> MockBus:
    return MockBus()


@pytest.fixture
def ctl(bus) -> ArmController:
    return ArmController(bus, max_step_deg=10, max_speed_dps=1000, settle_timeout_s=0.3)
