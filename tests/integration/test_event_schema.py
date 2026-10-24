"""QA.1: every event the product emits matches docs/INTERFACES.md §7.

The dashboard renders from the event stream alone (and replays a recorded events.jsonl as the
demo fallback), so an event type or field that drifts from §7 is a silent dashboard bug.

Three sources of events are checked:
- a real `make demo-fake` run (`cmd_demo_fake`, scripted author), read back from events.jsonl;
- the same repair loop driven by `DroidSDKAuthor` on the fake Droid SDK from tests/ws5, so the
  droid.* events (the award's "visible" criterion) are covered without the real SDK;
- every `.emit("<type>"` call site in src/, so types on paths neither run reaches are listed too.

Known mismatches live in KNOWN_GAPS with the owning workstream. Each one has a strict xfail test
that starts failing (XPASS) once the gap is fixed, so the entry gets removed.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from skillsmith.config import Config, load_config
from skillsmith.types import Classification, PhaseCheck, PrimitiveCall, TaskSpec

REPO = Path(__file__).resolve().parents[2]
INTERFACES = REPO / "docs" / "INTERFACES.md"
SRC = REPO / "src" / "skillsmith"


def _fields(cls) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


# Required `data` keys per event type, transcribed from INTERFACES §7. Extra keys are allowed.
SPEC: dict[str, set[str]] = {
    "run.started": {"task"},
    "scene.observed": {"scene", "image_path"},
    "plan.selected": {"skill", "source", "version"},
    "attempt.started": {"env", "skill", "version"},
    "attempt.finished": {"env", "skill", "version", "ok", "error", "duration_s"},
    "primitive.call": _fields(PrimitiveCall),
    "note": {"text"},
    "verify.result": {"ok", "detail"},
    "repair.started": {"round", "reason"},
    "droid.started": {"round", "via", "workspace", "autonomy", "disabled_tools"},
    "droid.tools": {"tools"},
    "droid.message": {"round", "text"},
    "droid.tool_call": {"round", "name", "input"},
    "droid.tool_result": {"round", "tool", "is_error", "content"},
    "droid.permission": {"round", "action", "target", "allowed", "reason"},
    "droid.check": {"round", "tool", "ok", "run", "reason"},
    "droid.result": {"round", "subtype", "result", "error", "session_id", "duration", "credits"},
    "droid.lesson": {"round", "lesson"},
    "droid.event": set(),  # CLI fallback: the raw stream-json event, no fixed keys
    "droid.finished": {"round", "session_id", "exit_code"},
    "skill.diff": {"old_version", "new_version", "diff"},
    "gate.result": {"gate", "ok", "detail"},
    "sim.video": {"path"},
    "approval.requested": {"summary"},
    "approval.decided": {"approved", "note"},
    "skill.saved": {"skill", "version"},
    "run.finished": {"ok", "rounds"},
    "phase.check": _fields(PhaseCheck),
    "attempt.classified": _fields(Classification),
    "world.bumped": {"message"},
    "error": {"where", "message"},
}

# Keys of nested values that §7 spells out.
NESTED: dict[tuple[str, str], set[str]] = {
    ("run.started", "task"): _fields(TaskSpec),
    ("droid.result", "result"): {"diagnosis", "change", "sim_checked", "confidence", "lesson"},
}

# Allowed values where §7 lists them.
ENUMS: dict[tuple[str, str], set] = {
    ("plan.selected", "source"): {"library", "draft"},
    ("gate.result", "gate"): {"static", "sim", "safety"},
    ("droid.check", "tool"): {"validate_skill", "simulate_skill"},
}

# Mismatches found by this suite, keyed by the violation string `_violations` produces.
KNOWN_GAPS: dict[str, str] = {
    "droid.check: missing data keys ['reason', 'run']":
        "ws5: RepairTools.validate_skill emits droid.check without `run`/`reason` "
        "(src/skillsmith/repair/droid_tools.py); emit run=None, reason=<first issue or 'ok'>, or the lead "
        "marks them sim-only in §7",
    "reset.requested: type not in INTERFACES §7":
        "ws5/lead: RepairLoop._reset_scene emits reset.requested (src/skillsmith/repair/loop.py), "
        "which §7 does not list; the lead adds `reset.requested | message` to §7",
}


# ---- the checker --------------------------------------------------------------------------

def _violations(events: list[dict]) -> set[str]:
    """Every way `events` departs from §7, as stable one-line strings."""
    out: set[str] = set()
    run_ids = {e.get("run_id") for e in events}
    if len(run_ids) > 1:
        out.add(f"envelope: one run has several run_ids {sorted(map(str, run_ids))}")
    last_t = float("-inf")
    for e in events:
        if set(e) != {"t", "run_id", "type", "data"}:
            out.add(f"envelope: keys {sorted(e)} != ['data', 'run_id', 't', 'type']")
            continue
        kind, data = e["type"], e["data"]
        if not isinstance(e["t"], float) or not isinstance(e["run_id"], str) or not isinstance(data, dict):
            out.add(f"{kind}: envelope has wrong value types")
            continue
        if e["t"] < last_t:
            out.add(f"{kind}: timestamp goes backwards")
        last_t = e["t"]
        if kind not in SPEC:
            out.add(f"{kind}: type not in INTERFACES §7")
            continue
        missing = SPEC[kind] - set(data)
        if missing:
            out.add(f"{kind}: missing data keys {sorted(missing)}")
        for (k, key), want in NESTED.items():
            value = data.get(key)
            if k == kind and value is not None:
                if not isinstance(value, dict) or want - set(value):
                    out.add(f"{kind}.{key}: missing keys {sorted(want - set(value or {}))}")
        for (k, key), allowed in ENUMS.items():
            if k == kind and key in data and data[key] not in allowed:
                out.add(f"{kind}.{key}: {data[key]!r} not in {sorted(allowed)}")
    return out


def _unexpected(violations: set[str]) -> list[str]:
    return sorted(v for v in violations if v not in KNOWN_GAPS)


# ---- event sources ------------------------------------------------------------------------

def _cfg(tmp_path: Path) -> Config:
    data = copy.deepcopy(load_config().raw())
    data["repair"]["runs_dir"] = str(tmp_path / "runs")
    data["repair"]["droid_ws_root"] = str(tmp_path / "droid_ws")
    data["repair"]["lessons_file"] = str(tmp_path / "lessons.md")
    return Config(data)


def _demo_fake_events(tmp_path: Path) -> list[dict]:
    from skillsmith.cli import cmd_demo_fake

    assert cmd_demo_fake(Namespace(author="scripted"), _cfg(tmp_path)) == 0
    (log,) = (tmp_path / "runs").glob("*/events.jsonl")
    return [json.loads(line) for line in log.read_text().splitlines()]


def _droid_sdk_events(tmp_path: Path, monkeypatch) -> list[dict]:
    """The demo loop with DroidSDKAuthor on the fake SDK: draft, one refused action, repair, lesson."""
    sys.path.insert(0, str(REPO / "tests" / "ws5"))
    try:
        import fake_droid_sdk as fake
    finally:
        sys.path.remove(str(REPO / "tests" / "ws5"))
    from skillsmith.demo import demo_sources, temp_library
    from skillsmith.events import NullBus
    from skillsmith.hal.base import Env, make_env
    from skillsmith.hal.fake import FakeWorld
    from skillsmith.repair import droid_sdk as author_mod
    from skillsmith.repair.approvers import AutoApprover
    from skillsmith.repair.droid_sdk import DroidSDKAuthor
    from skillsmith.repair.loop import RepairLoop
    from skillsmith.skills.library import SkillLibrary
    from skillsmith.tasks import parse_task

    droid = fake.install(monkeypatch)
    monkeypatch.setattr(author_mod.shutil, "which", lambda name: f"/opt/bin/{name}")
    v1, v2 = demo_sources()
    droid.turns = [
        fake.Turn(skill=v1, output=fake.output(), commands=["pytest"], questions=["which cup?"]),
        fake.Turn(skill=v2, output=fake.output("Grip cups at mid-height."), error_event="tool crashed"),
    ]
    cfg, bus = _cfg(tmp_path), NullBus()
    author = DroidSDKAuthor(cfg, bus, lambda s: make_env("fake", cfg, s))
    world = FakeWorld.demo(cfg, rim_slip=True)
    loop = RepairLoop(cfg, Env("fake", world, world, world), lambda s: make_env("fake", cfg, s),
                      SkillLibrary(temp_library()), author, AutoApprover("fake"), bus)
    try:
        result = loop.run(parse_task("stack the blue cup on the red cup"))
    finally:
        author.close()
    assert result.ok and result.rounds == 1
    return bus.events


def _doc_types() -> set[str]:
    """Event types in the §7 table: the backticked names in each row's first cell."""
    section = INTERFACES.read_text().split("## 7. Events", 1)[1].split("\n## ", 1)[0]
    rows = [line.split("|")[1] for line in section.splitlines() if line.startswith("| `")]
    return {name for cell in rows for name in re.findall(r"`([a-z_.]+)`", cell)}


def _emitted_types() -> dict[str, list[str]]:
    """Every literal event type passed to `.emit(` in src/, with where it is emitted."""
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for kind in re.findall(r"\.emit\(\s*\"([a-z_.]+)\"", line):
                found.setdefault(kind, []).append(f"{path.relative_to(REPO)}:{n}")
    return found


# ---- tests --------------------------------------------------------------------------------

def test_spec_matches_the_interfaces_table():
    assert _doc_types() == set(SPEC), "INTERFACES §7 changed: update SPEC in this file"


def test_spec_fields_appear_in_the_interfaces_table():
    section = INTERFACES.read_text().split("## 7. Events", 1)[1]
    dataclass_rows = {"primitive.call", "phase.check", "attempt.classified"}
    for kind, keys in SPEC.items():
        row = next(line for line in section.splitlines() if line.startswith("| ") and f"`{kind}`" in line.split("|")[1])
        for key in keys if kind not in dataclass_rows else ():
            assert re.search(rf"\b{re.escape(key)}\b", row), f"{kind}.{key} is not in the §7 row: {row}"


def test_demo_fake_events_conform(tmp_path):
    events = _demo_fake_events(tmp_path)
    assert _unexpected(_violations(events)) == []


def test_demo_fake_covers_the_core_story(tmp_path):
    seen = {e["type"] for e in _demo_fake_events(tmp_path)}
    story = {"run.started", "scene.observed", "plan.selected", "skill.diff", "gate.result", "approval.requested",
             "approval.decided", "attempt.started", "primitive.call", "verify.result", "attempt.finished",
             "repair.started", "skill.saved", "run.finished"}
    assert story - seen == set()


def _analysis_built() -> bool:
    from skillsmith.analysis import analyse_attempt

    try:
        analyse_attempt(None, None)
    except NotImplementedError:
        return False
    except Exception:
        return True
    return True


@pytest.mark.skipif(not _analysis_built(), reason="waiting on WS4.4/WS4.5 (analyse_attempt)")
def test_demo_fake_emits_phase_checks_and_a_classification(tmp_path):
    seen = {e["type"] for e in _demo_fake_events(tmp_path)}
    assert {"phase.check", "attempt.classified"} - seen == set()


def test_droid_sdk_events_conform(tmp_path, monkeypatch):
    events = _droid_sdk_events(tmp_path, monkeypatch)
    seen = {e["type"] for e in events}
    droid = {"droid.started", "droid.tools", "droid.message", "droid.tool_call", "droid.tool_result",
             "droid.permission", "droid.check", "droid.result", "droid.lesson", "error"}
    assert droid - seen == set()
    assert _unexpected(_violations(events)) == []


@pytest.mark.parametrize("kind", sorted(_emitted_types()))
def test_every_emitted_type_is_documented(kind):
    violation = f"{kind}: type not in INTERFACES §7"
    if violation in KNOWN_GAPS:
        pytest.xfail(KNOWN_GAPS[violation])
    assert kind in SPEC, f"{kind} emitted at {_emitted_types()[kind]} but not in INTERFACES §7"


def test_checker_catches_drift():
    good = {"t": 1.0, "run_id": "r", "type": "verify.result", "data": {"ok": True, "detail": "ok"}}
    assert _violations([good]) == set()
    assert _violations([{**good, "data": {"ok": True}}]) == {"verify.result: missing data keys ['detail']"}
    assert _violations([{**good, "type": "made.up"}]) == {"made.up: type not in INTERFACES §7"}
    bad_gate = {**good, "type": "gate.result", "data": {"gate": "vision", "ok": True, "detail": ""}}
    assert _violations([bad_gate]) == {"gate.result.gate: 'vision' not in ['safety', 'sim', 'static']"}
    assert _violations([good, {**good, "t": 0.5}]) == {"verify.result: timestamp goes backwards"}


# One strict xfail per known gap: XPASS (a failure) once the owner fixes it, so KNOWN_GAPS shrinks.
@pytest.mark.parametrize("gap", sorted(KNOWN_GAPS))
def test_known_gap_is_still_open(gap, tmp_path, monkeypatch):
    events = _droid_sdk_events(tmp_path, monkeypatch)
    violations = _violations(events) | {f"{k}: type not in INTERFACES §7" for k in _emitted_types() if k not in SPEC}
    if gap in violations:
        pytest.xfail(KNOWN_GAPS[gap])
    pytest.fail(f"fixed: remove from KNOWN_GAPS: {gap}")
