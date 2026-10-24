"""WS3.4 on track A's fixture frames (heavy job: needs cv2/numpy; YOLO is faked or skipped)."""

import json
import os
from pathlib import Path

import pytest

from skillsmith.perception.color import HsvDetector, YoloWorldDetector

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

FIXTURES = Path(__file__).resolve().parents[1] / "ws3" / "fixtures"
TRUTH = json.loads((FIXTURES / "truth.json").read_text())
SCENES = {f["file"]: f for f in TRUTH["frames"]}
LABELS = ["red cup", "blue cup", "green block"]
# In stacked.jpg the camera is almost straight over the stack: the blue cup hides the red one
# completely, so no detector can see it (the world model keeps it from the stack geometry).
HIDDEN = {("stacked.jpg", "red cup")}
# In stacked_far.jpg only a crescent of the red cup's side shows: found, but with low confidence.
OCCLUDED = {("stacked_far.jpg", "red cup")}
TOL_PX = 2.0


def frame(name):
    return cv2.imread(str(FIXTURES / name))


def dist(a, b):
    return float(np.hypot(a[0] - b[0], a[1] - b[1]))


@pytest.mark.parametrize("name", list(SCENES))
def test_hsv_finds_every_visible_object_with_its_label_and_top_centre(name):
    found = HsvDetector(None, LABELS).detect(frame(name))
    by_label = {}
    for d in found:
        by_label.setdefault(d.label, []).append(d)
    assert set(by_label) <= set(LABELS)
    for obj in SCENES[name]["objects"]:
        key = (name, obj["label"])
        if key in HIDDEN:
            assert obj["label"] not in by_label
            continue
        assert len(by_label.get(obj["label"], [])) == 1, f"{key}: {found}"
        d = by_label[obj["label"]][0]
        assert (d.color, d.kind, d.source) == (obj["color"], obj["kind"], "hsv")
        x0, y0, x1, y1 = d.bbox
        assert x0 < d.point_uv[0] < x1 and y0 < d.point_uv[1] < y1
        if key in OCCLUDED:
            assert d.confidence < 0.7
        else:
            assert d.confidence > 0.9
            assert dist(d.point_uv, obj["top_uv"]) < TOL_PX, f"{key}: {d.point_uv} vs {obj['top_uv']}"


@pytest.mark.parametrize("name", ["apart.jpg", "edges.jpg"])
def test_hsv_without_labels_tells_cups_from_blocks_by_shape(name):
    found = HsvDetector().detect(frame(name))  # markers and the grey table are not colours
    assert sorted(d.label for d in found) == sorted(o["label"] for o in SCENES[name]["objects"])


def test_hsv_sees_nothing_on_an_empty_table():
    table = np.full((480, 640, 3), (150, 155, 160), np.uint8)
    assert HsvDetector().detect(table) == []


class Boxes:
    def __init__(self, xyxy, conf, cls):
        self.xyxy, self.conf, self.cls = xyxy, conf, cls


class Result:
    def __init__(self, boxes, names):
        self.boxes, self.names = boxes, names


class LooseBoxYolo:
    """A fake YOLO returning loose boxes (side and a margin included) around each object."""

    def set_classes(self, labels):
        self.labels = list(labels)

    def predict(self, frame, conf, verbose):
        hsv = HsvDetector(None, LABELS).detect(frame)
        xyxy = np.array([[d.bbox[0] - 4, d.bbox[1] - 4, d.bbox[2] + 4, d.bbox[3] + 4] for d in hsv], float)
        cls = np.array([self.labels.index(d.label) for d in hsv])
        return [Result(Boxes(xyxy, np.full(len(cls), 0.8), cls), dict(enumerate(self.labels)))]


def test_yolo_boxes_are_refined_to_the_top_centre():
    out = YoloWorldDetector(None, LABELS, model=LooseBoxYolo()).detect(frame("edges.jpg"))
    truth = {o["label"]: o["top_uv"] for o in SCENES["edges.jpg"]["objects"]}
    assert sorted(d.label for d in out) == sorted(truth)
    for d in out:
        assert d.source == "yolo" and d.confidence == 0.8
        assert dist(d.point_uv, truth[d.label]) < TOL_PX, (d.label, d.point_uv, truth[d.label])


@pytest.mark.skipif(not os.environ.get("SKILLSMITH_YOLO_MODEL"), reason="SKILLSMITH_YOLO_MODEL=<YOLO-World/E .pt> unset")
def test_real_yolo_finds_the_fixture_objects():
    pytest.importorskip("ultralytics")
    cfg = {"detect": {"yolo": {"model": os.environ["SKILLSMITH_YOLO_MODEL"], "conf": 0.1}}}
    out = YoloWorldDetector(cfg, LABELS).detect(frame("apart.jpg"))
    assert {"red cup", "blue cup"} <= {d.label for d in out}
