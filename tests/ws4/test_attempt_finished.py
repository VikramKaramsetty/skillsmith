"""WS4.10: attempt.finished carries scene_before, scene_after, safety and perception (INTERFACES §7),
and every phase with a key frame gets it as `PhaseCheck.frame_path`."""

import dataclasses
import json

from skillsmith.config import REPO_ROOT, load_config
from skillsmith.demo import demo_sources
from skillsmith.events import EventBus, NullBus
from skillsmith.hal.base import Env
from skillsmith.hal.fake import FakeWorld
from skillsmith.skills.runtime import run_skill
from skillsmith.types import Scene

cfg = load_config()
LIBRARY = {"pick_place": (REPO_ROOT / "skills/library/pick_place.py").read_text()}
STACK = {"top": "blue cup", "bottom": "red cup"}


def _run(world, bus=None):
    v1, _ = demo_sources()
    bus = bus or NullBus()
    result = run_skill(v1, Env("fake", world, world, world), STACK, cfg, bus, skill="stack", library=LIBRARY)
    return result, bus


def _finished(bus) -> dict:
    [event] = [e for e in bus.events if e["type"] == "attempt.finished"]
    return event["data"]


def test_attempt_finished_carries_the_scenes_and_safety_report():
    result, bus = _run(FakeWorld.demo(cfg, rim_slip=True))
    data = _finished(bus)
    assert not data["ok"]
    assert Scene.from_dict(data["scene_before"]).objects == result.scene_before.objects
    assert Scene.from_dict(data["scene_after"]).objects == result.scene_after.objects
    assert data["safety"] == dataclasses.asdict(result.safety)
    assert data["perception"] is None  # the fake world has no perception_ok()


def test_successful_attempt_carries_them_too():
    _, bus = _run(FakeWorld.demo(cfg))
    data = _finished(bus)
    assert data["ok"]
    assert {"scene_before", "scene_after", "safety", "perception"} <= data.keys()
    assert data["safety"]["ok"] is True


def test_perception_gate_is_recorded_as_ok_and_reason():
    world = FakeWorld.demo(cfg, rim_slip=True)
    world.perception_ok = lambda: (False, "blue cup confidence 0.31 under 0.5")
    _, bus = _run(world)
    assert _finished(bus)["perception"] == [False, "blue cup confidence 0.31 under 0.5"]


def test_recorded_event_is_json_and_scene_rebuilds(tmp_path):
    bus = EventBus("rec", runs_dir=tmp_path)
    result, _ = _run(FakeWorld.demo(cfg, rim_slip=True), bus=_Recorder(bus))
    bus.close()
    [line] = [json.loads(x) for x in (tmp_path / "rec" / "events.jsonl").read_text().splitlines()
              if json.loads(x)["type"] == "attempt.finished"]
    assert Scene.from_dict(line["data"]["scene_after"]).objects == result.scene_after.objects
    assert line["data"]["safety"]["ok"] == result.safety.ok


def test_phase_checks_get_the_key_frame_of_their_phase():
    world = FakeWorld.demo(cfg, rim_slip=True)
    world.keyframes = lambda trace, phases: {"carry": "/frames/carry.jpg", "nonsense": "/frames/x.jpg"}
    result, bus = _run(world)
    by_phase = {p.phase: p for p in result.phases}
    assert by_phase["carry"].frame_path == "/frames/carry.jpg"
    assert all(p.frame_path is None for name, p in by_phase.items() if name != "carry")
    assert result.keyframes["carry"] == "/frames/carry.jpg"
    emitted = {e["data"]["phase"]: e["data"] for e in bus.events if e["type"] == "phase.check"}
    assert emitted["carry"]["frame_path"] == "/frames/carry.jpg"


def test_broken_keyframe_renderer_is_reported_not_fatal():
    world = FakeWorld.demo(cfg, rim_slip=True)

    def broken(trace, phases):
        raise RuntimeError("no GL context")
    world.keyframes = broken
    result, bus = _run(world)
    assert result.phases and all(p.frame_path is None for p in result.phases)
    errors = [e["data"] for e in bus.events if e["type"] == "error"]
    assert any(e["where"] == "keyframes" and "no GL context" in e["message"] for e in errors)
    assert _finished(bus)["safety"] is not None


class _Recorder(NullBus):
    """NullBus that also writes through to a real bus, so one run gives both memory and a file."""

    def __init__(self, inner: EventBus):
        super().__init__()
        self.subscribe(lambda e: inner.emit(e["type"], **e["data"]))
