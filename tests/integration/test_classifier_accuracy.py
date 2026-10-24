"""QA.5: classifier accuracy on recorded real failures, scored against hand labels.

The classifier decides who fixes a failure: a skill label sends it to Droid, an environment label
to a human. A wrong label is worse than UNKNOWN, so the score charges a wrong label more than an
abstention, and an environment failure labelled as a skill failure is a hard fail.

Three layers, so the harness is proven before the hardware data exists:
- the replay path: a fake-world run written to events.jsonl rebuilds into the same AttemptResult
  the runtime handed the classifier, so re-classifying a recording is faithful;
- the scoring: precision per class, UNKNOWN as an abstention, environment-to-Droid misroutes;
- the gates: hand-labelled fake recordings, then the real corpus in recorded_failures/
  (skips with "needs hardware runs" until a human adds one). Both skip until WS4.5 lands.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest
from qa_classifier_accuracy import (
    NO_LABEL,
    UNKNOWN,
    WRONG_PENALTY,
    AccuracyReport,
    analysis_classifier,
    attempts_from_events,
    evaluate,
    load_corpus,
    read_events,
)

from skillsmith.config import REPO_ROOT, load_config
from skillsmith.demo import demo_sources
from skillsmith.events import EventBus
from skillsmith.hal.base import Env
from skillsmith.hal.fake import FakeWorld
from skillsmith.skills.runtime import run_skill
from skillsmith.types import FAILURE_CLASSES, AttemptResult, Classification

cfg = load_config()
RECORDED = Path(__file__).parent / "recorded_failures"
PICK_PLACE = (REPO_ROOT / "skills/library/pick_place.py").read_text()
COLLIDING_PICK_PLACE = PICK_PLACE.replace("place_z + grip_offset + 0.005", "place_z + 0.01")
STACK = {"op": "on_top", "args": ["blue cup", "red cup"]}
CUPS = {"top": "blue cup", "bottom": "red cup"}

MIN_PRECISION = 0.8  # per predicted class on the real corpus
DEMO_CLASS = "SLIP_IN_CARRY"  # G5: the demo rides on this label being right every time


# ---- recording fake runs the way the repair loop does ------------------------------------

def record(runs: Path, run_id: str, source: str, params: dict, *, rim_slip: bool = False,
           success: Optional[dict] = None, skill: str = "t") -> AttemptResult:
    """Run a skill on the fake world with a file-backed bus, observing first like RepairLoop._observe."""
    world = FakeWorld.demo(cfg, rim_slip=rim_slip)
    bus = EventBus(run_id, runs_dir=runs)
    try:
        scene = world.observe()
        bus.emit("scene.observed", scene=scene, image_path=scene.image_path)
        return run_skill(source, Env("fake", world, world, world), params, cfg, bus, skill=skill,
                         library={"pick_place": PICK_PLACE}, success=success)
    finally:
        bus.close()


def _skill(body: str) -> str:
    return f"SKILL = {{}}\n\ndef run(robot, scene, skills):\n    {body}\n"


# Hand-labelled fake failures: (run id, label, how to produce it). Labels come from what the
# fake world does, written by hand, never from the classifier.
FAKE_CASES = [
    ("slip", "SLIP_IN_CARRY",
     lambda runs: record(runs, "slip", demo_sources()[0], CUPS, rim_slip=True, success=STACK, skill="stack")),
    ("collision", "COLLISION",
     lambda runs: record(runs, "collision", COLLIDING_PICK_PLACE, {"obj": "blue cup", "x": 0.22, "y": 0.08},
                         success=STACK, skill="pick_place")),
    ("unsafe", "UNSAFE_TARGET",
     lambda runs: record(runs, "unsafe", _skill("robot.move_to(0.6, 0.0, 0.1)"), {})),
    ("not_found", "OBJECT_NOT_FOUND",
     lambda runs: record(runs, "not_found", _skill('scene.require("purple cup")'), {})),
]


@pytest.fixture(scope="module")
def fake_corpus(tmp_path_factory) -> Path:
    runs = tmp_path_factory.mktemp("recorded_fake")
    lines = []
    for run_id, label, produce in FAKE_CASES:
        assert not produce(runs).ok, f"fake case {run_id} was meant to fail"
        lines.append(f"- run: {run_id}\n  label: {label}\n")
    (runs / "labels.yaml").write_text("".join(lines))
    return runs


def _product_classifier():
    """analysis.analyse_attempt as a Classifier, or skip while WS4.5 is a stub."""
    classify = analysis_classifier(cfg)

    def safe(attempt):
        try:
            return classify(attempt)
        except NotImplementedError:
            pytest.skip("waiting on WS4.4/WS4.5: analysis.analyse_attempt is not implemented")
    return safe


# ---- 1. replay path: events.jsonl → AttemptResult ----------------------------------------

def test_recorded_attempt_rebuilds_what_the_classifier_saw(tmp_path):
    live = record(tmp_path, "slip", demo_sources()[0], CUPS, rim_slip=True, success=STACK, skill="stack")
    [replayed] = attempts_from_events(read_events(tmp_path / "slip" / "events.jsonl"))

    assert not live.ok and "lost blue cup" in live.error
    for attr in ("ok", "env", "skill", "version", "error", "duration_s"):
        assert getattr(replayed, attr) == getattr(live, attr), attr
    assert replayed.verify == live.verify
    assert len(replayed.trace) == len(live.trace)
    for got, want in zip(replayed.trace, live.trace):
        assert (got.index, got.name, got.args, got.ok, got.error) == (want.index, want.name, want.args, want.ok, want.error)
        assert got.ee_before == want.ee_before and got.ee_after == want.ee_after
        # primitive.call keeps scalar results (grasp -> bool, align_over -> float) and shortens the
        # rest to "…"; a look()'s Scene comes back through its own scene.observed event instead.
        scalar = isinstance(want.result, (bool, int, float, str, type(None)))
        assert got.result == (want.result if scalar else "…")
    assert [c.result for c in replayed.trace if c.name == "grasp"] == [True]
    assert replayed.scene_before.objects == live.scene_before.objects


@pytest.mark.xfail(reason="lead: attempt.finished carries no scene_after or safety report, so a recording "
                          "loses them (see HANDOFF CONTRACT CHANGE REQUEST)")
def test_recorded_attempt_keeps_scene_after_and_safety(tmp_path):
    live = record(tmp_path, "collide", COLLIDING_PICK_PLACE, {"obj": "blue cup", "x": 0.22, "y": 0.08},
                  success=STACK, skill="pick_place")
    [replayed] = attempts_from_events(read_events(tmp_path / "collide" / "events.jsonl"))
    assert not live.safety.ok
    assert replayed.safety.ok == live.safety.ok
    assert replayed.scene_after is not None and replayed.scene_after.objects == live.scene_after.objects


def test_attempt_windows_split_a_multi_attempt_run():
    def ev(type_, **data):
        return {"t": 0.0, "run_id": "r", "type": type_, "data": data}

    def scene(x):
        return {"objects": [{"id": "c", "label": "blue cup", "color": "blue", "kind": "cup", "x": x, "y": 0.0,
                             "z": 0.0, "height": 0.08, "width": 0.065}], "t": 0.0, "source": "real"}

    call = {"index": 0, "name": "grasp", "args": {}, "t": 0.0, "ee_before": [0.2, 0.0, 0.05],
            "ee_after": [0.2, 0.0, 0.05], "ok": True, "result": False, "error": None}
    events = [
        ev("run.started", task={}), ev("scene.observed", scene=scene(0.20), image_path=None),
        ev("attempt.started", env="real", skill="stack", version=1), ev("primitive.call", **call),
        ev("verify.result", ok=False, detail="blue cup not on red cup"),
        ev("attempt.finished", env="real", skill="stack", version=1, ok=False, error=None, duration_s=4.0),
        ev("repair.started", round=1, reason="x"), ev("scene.observed", scene=scene(0.25), image_path=None),
        ev("attempt.started", env="real", skill="stack", version=2),
        ev("attempt.finished", env="real", skill="stack", version=2, ok=True, error=None, duration_s=3.0),
        ev("attempt.started", env="real", skill="stack", version=3),  # cut off: never finished
    ]
    first, second = attempts_from_events(events)
    assert (first.version, first.ok, second.version, second.ok) == (1, False, 2, True)
    assert [c.name for c in first.trace] == ["grasp"] and first.trace[0].ee_before == (0.2, 0.0, 0.05)
    assert first.verify.detail == "blue cup not on red cup" and second.verify is None
    assert first.scene_before.objects[0].x == 0.20 and second.scene_before.objects[0].x == 0.25
    assert second.trace == []


# ---- 2. scoring ----------------------------------------------------------------------------

def _report(*rows) -> AccuracyReport:
    report = AccuracyReport()
    for truth, pred in rows:
        report.add("r", truth, pred)
    return report


def test_unknown_is_an_abstention_and_wrong_costs_more():
    honest = _report(("SLIP_IN_CARRY", "SLIP_IN_CARRY"), ("GRASP_MISS", UNKNOWN))
    guessing = _report(("SLIP_IN_CARRY", "SLIP_IN_CARRY"), ("GRASP_MISS", "SLIP_IN_CARRY"))
    assert (honest.correct, honest.unknown, honest.wrong, honest.score) == (1, 1, 0, 1)
    assert (guessing.correct, guessing.unknown, guessing.wrong) == (1, 0, 1)
    assert guessing.score == 1 - WRONG_PENALTY < honest.score

    assert honest.per_class()["SLIP_IN_CARRY"].precision == 1.0
    assert honest.per_class()["GRASP_MISS"].abstained == 1 and honest.per_class()["GRASP_MISS"].recall == 0.0
    assert UNKNOWN not in honest.per_class()  # abstentions are not a predicted class
    assert guessing.per_class()["SLIP_IN_CARRY"].precision == 0.5


def test_environment_failure_labelled_as_skill_is_flagged():
    report = _report(("CAMERA_MOVED", "SLIP_IN_CARRY"), ("CAMERA_MOVED", "PERCEPTION_UNCERTAIN"),
                     ("OBJECT_NOT_FOUND", UNKNOWN), ("GRASP_MISS", "CAMERA_MOVED"), ("TIMEOUT", NO_LABEL))
    assert [(t, p) for _, t, p, _ in report.env_sent_to_droid()] == [("CAMERA_MOVED", "SLIP_IN_CARRY")]
    assert len(report.missing()) == 1 and report.wrong == 4
    text = report.markdown()
    assert "environment failures sent to Droid as skill: 1" in text
    assert "| CAMERA_MOVED | SLIP_IN_CARRY | 1 |" in text


def test_load_corpus_reads_labels_and_rejects_bad_ones(tmp_path):
    assert load_corpus(tmp_path) == []
    record(tmp_path, "slip", demo_sources()[0], CUPS, rim_slip=True, success=STACK, skill="stack")
    record(tmp_path, "fine", demo_sources()[0], CUPS, success=STACK, skill="stack")

    (tmp_path / "labels.yaml").write_text("- {run: slip, label: SLIP_IN_CARRY, note: rim grip}\n")
    [case] = load_corpus(tmp_path)
    assert (case.run, case.label, case.note, case.attempt.ok) == ("slip", "SLIP_IN_CARRY", "rim grip", False)

    (tmp_path / "labels.yaml").write_text("- {run: slip, label: CUP_FELL_OVER}\n")
    with pytest.raises(ValueError, match="unknown class"):
        load_corpus(tmp_path)
    (tmp_path / "labels.yaml").write_text("- {run: fine, label: SLIP_IN_CARRY}\n")
    with pytest.raises(ValueError, match="no failed attempt"):
        load_corpus(tmp_path)


def test_checked_in_real_corpus_parses():
    for case in load_corpus(RECORDED):  # empty until hardware runs; must never be malformed
        assert not case.attempt.ok


def test_evaluate_scores_a_classifier_over_a_corpus(fake_corpus):
    corpus = load_corpus(fake_corpus)
    assert [c.label for c in corpus] == [label for _, label, _ in FAKE_CASES]

    def stub(attempt: AttemptResult) -> Optional[Classification]:
        cls = "SLIP_IN_CARRY" if "lost" in (attempt.error or "") else UNKNOWN
        return Classification(cls, FAILURE_CLASSES[cls], None, None, attempt.error or "")

    report = evaluate(corpus, stub)
    assert (report.correct, report.unknown, report.wrong) == (1, 3, 0)


# ---- 3. gates: the product classifier on labelled recordings --------------------------------

def _assert_gates(report: AccuracyReport, *, min_precision: float) -> None:
    text = "\n" + report.markdown()
    assert not report.missing(), f"failed attempts with no classification: {report.missing()}{text}"
    assert not report.env_sent_to_droid(), f"environment failures labelled as skill: {report.env_sent_to_droid()}{text}"
    low = {s.cls: s.precision for s in report.per_class().values()
           if s.precision is not None and s.precision < min_precision}
    assert not low, f"precision below {min_precision:.0%}: {low}{text}"
    demo = report.per_class().get(DEMO_CLASS)
    assert demo is None or demo.recall in (None, 1.0), f"{DEMO_CLASS} missed on some recorded case(s){text}"
    assert report.score > 0, f"score {report.score}: wrong labels outweigh right ones{text}"


def test_classifier_on_hand_labelled_fake_recordings(fake_corpus):
    """Unambiguous fake failures, replayed from events.jsonl: nothing may be labelled wrong."""
    report = evaluate(load_corpus(fake_corpus), _product_classifier())
    print(report.markdown())
    _assert_gates(report, min_precision=1.0)
    assert report.wrong == 0, report.markdown()


def test_classifier_on_recorded_real_failures():
    corpus = load_corpus(RECORDED)
    if not corpus:
        pytest.skip("no recorded real failures yet: needs hardware runs (see recorded_failures/README.md)")
    not_real = [c.run for c in corpus if c.attempt.env != "real"]
    assert not not_real, f"recorded_failures/ is for real-arm runs only: {not_real}"
    report = evaluate(corpus, _product_classifier())
    print(report.markdown())
    _assert_gates(report, min_precision=MIN_PRECISION)
