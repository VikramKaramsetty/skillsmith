"""An object too near the base for a side grasp is a table problem (OBJECT_OUT_OF_REACH), not a skill bug."""

from skillsmith.analysis.classify import _too_near


class _F:
    def __init__(self, kind, message):
        self.kind, self.message, self.stop = kind, message, None


def test_side_grasp_refusal_and_skill_raise_are_out_of_reach(monkeypatch):
    import importlib

    c = importlib.import_module("skillsmith.analysis.classify")

    monkeypatch.setattr(c, "_label", lambda f, cls, *a, **k: cls)
    assert _too_near(_F("Unreachable", "pink cup at (0.2, 0.0) is too near the base to reach in from the side: "
                                       "... Move it outward")) == "OBJECT_OUT_OF_REACH"
    assert _too_near(_F("SkillError", "pink cup is 0.196 m from the base; a side grasp needs 0.28 m. "
                                      "Move the cup outward before running move_cup")) == "OBJECT_OUT_OF_REACH"
    assert _too_near(_F("SkillError", "pink cup is 0.333 m from the base; side grasp needs 0.28 to 0.31 m")) \
        == "OBJECT_OUT_OF_REACH"
    assert _too_near(_F("SkillError", "side grasp missed pink cup")) is None
    assert _too_near(_F("ValueError", "0.2 m from the base, move it outward")) is None
