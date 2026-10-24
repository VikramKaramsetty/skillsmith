"""Every workstream module imports with only core deps (heavy libraries must be imported lazily)."""

import importlib

import pytest

MODULES = [
    "skillsmith.kinematics",
    "skillsmith.hal.sim",
    "skillsmith.hal.real",
    "skillsmith.perception.geometry",
    "skillsmith.perception.world",
    "skillsmith.perception.annotate",
    "skillsmith.analysis",
    "skillsmith.perception.color",
    "skillsmith.dashboard.server",
    "skillsmith.dashboard.approver",
    "skillsmith.calibration",
]


@pytest.mark.parametrize("name", MODULES)
def test_imports_without_heavy_deps(name):
    importlib.import_module(name)
