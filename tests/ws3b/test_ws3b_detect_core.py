"""WS3.4: the detector interface without cv2/numpy/ultralytics (runs in `make check` anywhere)."""

import pytest

from skillsmith.perception import color
from skillsmith.perception.color import (
    Detection,
    FallbackDetector,
    HsvDetector,
    YoloWorldDetector,
    make_detector,
    split_label,
)


class FakeBus:
    def __init__(self):
        self.events = []

    def emit(self, type_, **data):
        self.events.append((type_, data))


class Boxes:
    def __init__(self, xyxy, conf, cls):
        self.xyxy, self.conf, self.cls = xyxy, conf, cls


class Result:
    def __init__(self, boxes, names):
        self.boxes, self.names = boxes, names


class FakeYolo:
    """Stands in for an ultralytics YOLOWorld model: records the prompt, returns canned boxes."""

    def __init__(self, boxes):
        self.boxes = boxes
        self.classes = None
        self.calls = []

    def set_classes(self, labels):
        self.classes = list(labels)

    def predict(self, frame, conf, verbose):
        self.calls.append(conf)
        names = dict(enumerate(self.classes))
        return [Result(Boxes(*self.boxes), names)]


class FakeYoloE(FakeYolo):
    def get_text_pe(self, labels):
        return f"pe:{len(labels)}"

    def set_classes(self, labels, pe):
        self.classes, self.pe = list(labels), pe


class Fixed:
    def __init__(self, dets=(), error=None):
        self.dets, self.error, self.calls = list(dets), error, 0

    def detect(self, frame_bgr):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.dets)


def det(label, conf, source="hsv"):
    c, k = split_label(label)
    return Detection(label, c, k, (0, 0, 10, 10), (5.0, 5.0), conf, source)


def test_split_label():
    assert split_label("blue cup") == ("blue", "cup")
    assert split_label("Light Blue  Cup") == ("light blue", "cup")
    assert split_label("cup") == ("", "cup")
    assert split_label("") == ("", "")


def test_hsv_detector_takes_kinds_from_labels_and_config_overrides():
    cfg = {"detect": {"hsv": {"min_area": 50, "hues": {"red": [[0, 9], [170, 179]], "blue": [[95, 130]]}}}}
    d = HsvDetector(cfg, ["red cup", "blue cup", "blue block", "mug"])
    assert d.params["min_area"] == 50 and d.params["min_s"] == color.DEFAULT_HSV["min_s"]
    assert d.colors == ["red", "blue"]
    assert d.kinds == {"red": {"cup"}, "blue": {"cup", "block"}}
    assert d.unseen == ["mug"]
    assert d._kind("red", hole=0.0) == "cup"  # the only red thing on the table is a cup
    assert d._kind("blue", hole=0.7) == "cup" and d._kind("blue", hole=0.0) == "block"
    assert HsvDetector().colors == list(color.DEFAULT_HUES)  # no labels: every known colour


def test_yolo_detector_maps_boxes_to_detections_sorted_by_confidence():
    model = FakeYolo(([[10, 20, 50, 60], [100.4, 100.6, 140, 150]], [0.4, 0.9], [0, 1]))
    d = YoloWorldDetector({"detect": {"yolo": {"conf": 0.3}}}, ["widget", "big mug"], model=model)
    assert model.classes == ["widget", "big mug"]
    out = d.detect(None)  # no colour word: the top point is the box centre, the frame is never read
    assert model.calls == [0.3]
    assert [x.label for x in out] == ["big mug", "widget"]
    assert out[0] == Detection("big mug", "big", "mug", (100, 101, 140, 150), (120.0, 125.5), 0.9, "yolo")
    assert out[1].point_uv == (30.0, 40.0) and out[1].color == "" and out[1].kind == "widget"


def test_yoloe_gets_text_embeddings_and_labels_are_required():
    model = FakeYoloE(([], [], []))
    YoloWorldDetector(None, ["red cup"], model=model)
    assert model.classes == ["red cup"] and model.pe == "pe:1"
    with pytest.raises(ValueError, match="at least one label"):
        YoloWorldDetector(None, [], model=model)


def test_fallback_adds_labels_the_primary_missed():
    primary = Fixed([det("blue cup", 0.8, "yolo")])
    backup = Fixed([det("blue cup", 0.99), det("green block", 0.95)])
    out = FallbackDetector(primary, backup).detect(None)
    assert [(d.label, d.source) for d in out] == [("green block", "hsv"), ("blue cup", "yolo")]


def test_fallback_uses_backup_when_primary_raises_and_reports_once():
    bus = FakeBus()
    primary = Fixed(error=RuntimeError("CUDA out of memory"))
    backup = Fixed([det("red cup", 0.9)])
    f = FallbackDetector(primary, backup, bus)
    assert [d.label for d in f.detect(None)] == ["red cup"]
    assert [d.label for d in f.detect(None)] == ["red cup"]
    assert len(bus.events) == 1
    type_, data = bus.events[0]
    assert type_ == "error" and data["where"] == "perception.detector" and "CUDA out of memory" in data["message"]
    primary.error = None
    f.detect(None)
    assert "recovered" in bus.events[-1][1]["message"] and len(bus.events) == 2


def test_make_detector_backends(monkeypatch):
    assert isinstance(make_detector({"detect": {"backend": "hsv"}}, ["red cup"]), HsvDetector)
    assert isinstance(make_detector(None), HsvDetector)  # auto with no labels: nothing to prompt YOLO with
    with pytest.raises(ValueError, match="backend"):
        make_detector({"detect": {"backend": "magic"}})

    def no_ultralytics(path):
        raise ImportError("no ultralytics")

    monkeypatch.setattr(color, "_load_yolo", no_ultralytics)
    bus = FakeBus()
    assert isinstance(make_detector(None, ["red cup"], bus), HsvDetector)
    assert bus.events[0][0] == "error" and "no ultralytics" in bus.events[0][1]["message"]
    with pytest.raises(ImportError):
        make_detector({"detect": {"backend": "yolo"}}, ["red cup"])

    monkeypatch.setattr(color, "_load_yolo", lambda path: FakeYolo(([], [], [])))
    chain = make_detector(None, ["red cup"])
    assert isinstance(chain, FallbackDetector) and isinstance(chain.primary, YoloWorldDetector)


def test_make_detector_reads_a_config_object():
    from skillsmith.config import load_config

    d = make_detector(load_config(), [])  # robot.yaml has no detect section yet: defaults apply
    assert isinstance(d, HsvDetector) and d.params == color.DEFAULT_HSV
