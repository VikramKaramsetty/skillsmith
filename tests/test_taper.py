"""SceneObject.top_width: a tapered object's width at a height (the demo's upside-down cup)."""

from skillsmith.types import Scene, SceneObject

CUP = SceneObject("c", "pink cup", "pink", "cup", 0.25, 0.0, 0.0, 0.0953, 0.0762, top_width=0.0508)


def test_width_interpolates_base_to_top_and_clamps():
    assert CUP.width_at(0.0) == 0.0762
    assert abs(CUP.width_at(CUP.top) - 0.0508) < 1e-12
    assert abs(CUP.width_at(CUP.height / 2) - 0.0635) < 1e-12
    assert CUP.width_at(-1.0) == 0.0762 and abs(CUP.width_at(1.0) - 0.0508) < 1e-12


def test_untapered_width_is_constant_and_old_dicts_still_load():
    block = SceneObject("b", "blue block", "blue", "block", 0.2, 0.0, 0.0, 0.03, 0.03)
    assert block.width_at(0.02) == 0.03
    old = {"objects": [{"id": "b", "label": "blue block", "color": "blue", "kind": "block", "x": 0.2, "y": 0.0,
                        "z": 0.0, "height": 0.03, "width": 0.03, "confidence": 1.0, "visible": True}],
           "t": 0.0, "source": "test"}
    assert Scene.from_dict(old).objects[0].top_width is None
