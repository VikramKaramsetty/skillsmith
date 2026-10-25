import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("tip", Path(__file__).resolve().parents[1] / "tools" / "tip.py")
tip = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tip)


def test_markers_are_merged_sorted_and_rounded(tmp_path):
    f = tmp_path / "markers.json"
    tip.save_marker(f, 2, (0.123456, -0.05))
    tip.save_marker(f, 0, (0.1, 0.15))
    tip.save_marker(f, 2, (0.2, -0.06))  # re-touching a marker replaces it
    assert json.loads(f.read_text()) == {"0": [0.1, 0.15], "2": [0.2, -0.06]}
    assert list(json.loads(f.read_text())) == ["0", "2"]
