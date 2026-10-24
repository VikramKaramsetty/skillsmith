import importlib.util

from skillsmith.config import REPO_ROOT

spec = importlib.util.spec_from_file_location("check_ownership", REPO_ROOT / "scripts" / "check_ownership.py")
own = importlib.util.module_from_spec(spec)
spec.loader.exec_module(own)
OWNERSHIP = own.load_ownership()


def test_in_lane():
    assert own.violations("ws2/scene", ["src/skillsmith/hal/sim.py", "tests/ws2/test_sim.py"], OWNERSHIP) == []


def test_out_of_lane():
    problems = own.violations("ws2/scene", ["src/skillsmith/types.py"], OWNERSHIP)
    assert len(problems) == 1 and "outside ws2" in problems[0]


def test_lead_and_unknown_prefix():
    assert own.violations("lead/contracts", ["src/skillsmith/types.py"], OWNERSHIP) == []
    assert "must start with" in own.violations("feature/x", ["README.md"], OWNERSHIP)[0]


def test_every_workstream_has_paths():
    for name, ws in OWNERSHIP["workstreams"].items():
        assert ws["paths"], name
