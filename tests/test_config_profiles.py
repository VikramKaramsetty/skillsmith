"""Config profiles (WS5.7 stage profile) and the seeded demo story."""

import pytest

from skillsmith.cli import main
from skillsmith.config import load_config


def test_stage_profile_overlays_robot_yaml():
    base, stage = load_config(), load_config(profile="stage")
    assert stage.repair.max_rounds == 2 and stage.droid.sim_tool_budget == 1 and stage.droid.timeout_s == 90
    assert stage.profile == "stage"
    # untouched keys survive the deep merge
    assert stage.droid.restrict_tools == base.droid.restrict_tools
    assert stage.workspace.max_reach == base.workspace.max_reach


def test_unknown_profile_is_an_error():
    with pytest.raises(FileNotFoundError):
        load_config(profile="nope")


def test_seeded_demo_fails_honestly_then_recovers(capsys):
    """--seed starts from the block-tuned stack: the rim grip slips on the cup, then the repair works."""
    assert main(["--profile", "stage", "demo-fake", "--seed"]) == 0
    out = capsys.readouterr().out
    assert "block-tuned stack seed" in out and "Profile: stage" in out
    assert "SUCCESS after 1 repair round(s)" in out
