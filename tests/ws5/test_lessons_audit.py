"""WS5.8: lessons in action (the right past lessons reach the next repair, visibly) and an audit
trail (record + optional git commit) for every saved skill version. Fake world, no network."""

import copy
import json
import shutil
import subprocess
from pathlib import Path

import fake_droid_sdk as fake
import pytest

from skillsmith.config import Config, load_config
from skillsmith.demo import demo_sources, temp_library
from skillsmith.events import NullBus
from skillsmith.hal.base import Env, make_env
from skillsmith.hal.fake import FakeWorld
from skillsmith.repair import audit, lessons
from skillsmith.repair import droid_sdk as author_mod
from skillsmith.repair.approvers import AutoApprover
from skillsmith.repair.droid_sdk import DroidSDKAuthor
from skillsmith.repair.loop import RepairLoop
from skillsmith.repair.protocols import AuthorContext
from skillsmith.repair.scripted import ScriptedAuthor
from skillsmith.skills.library import SkillLibrary
from skillsmith.skills.runtime import run_skill
from skillsmith.tasks import parse_task
from skillsmith.types import Classification

TASK = parse_task("stack the blue cup on the red cup")
V1, V2 = demo_sources()
SLIP = Classification("SLIP_IN_CARRY", "skill", "carry", 6,
                      "grasp at 88% of cup height held, lost during a 16 cm sideways move", hint="grip height")
RELEVANT = "Grip cups at 40-60% of their height; rim grips slip in carry."
OLD = f"- 2026-10-01 (stack the red block on the green block; skill stack; SLIP_IN_CARRY in carry): {RELEVANT}\n"
NOISE = "".join(f"- 2026-10-{10 + i} (sort the blocks by color; skill sort_by_color): Look again after pick {i}.\n"
                for i in range(15))
LESSONS = "# Lessons Droid has learned\n\n" + OLD + NOISE  # the relevant one is the OLDEST of 16


def tmp_cfg(tmp_path, **repair) -> Config:
    data = copy.deepcopy(load_config().raw())
    data["repair"]["droid_ws_root"] = str(tmp_path / "droid_ws")
    data["repair"]["lessons_file"] = str(tmp_path / "lessons.md")
    data["repair"].update(repair)
    return Config(data)


def classified_attempt(cfg):
    w = FakeWorld.demo(cfg, rim_slip=True)
    attempt = run_skill(V1, Env("fake", w, w, w), TASK.params, cfg, NullBus(), skill="stack", version=0,
                        success=TASK.success)
    attempt.classification = copy.deepcopy(SLIP)
    return attempt


def make_loop(cfg, author, bus, library_dir=None, classify=True):
    w = FakeWorld.demo(cfg, rim_slip=True)
    library = SkillLibrary(library_dir or temp_library())
    loop = RepairLoop(cfg, Env("fake", w, w, w), lambda s: make_env("fake", cfg, s), library,
                      author, AutoApprover("fake"), bus)
    if classify:  # WS4's classifier is not wired into the fake world yet: label the target's failures
        attempt_fn = loop._attempt

        def labelled(task, source, rnd):
            result = attempt_fn(task, source, rnd)
            if not result.ok:
                result.classification = copy.deepcopy(SLIP)
            return result
        loop._attempt = labelled
    return loop, library


def events(bus, kind):
    return [e["data"] for e in bus.events if e["type"] == kind]


# ---- selection -----------------------------------------------------------------------------
def test_parse_reads_context_and_text():
    (first, *rest) = lessons.parse(LESSONS)
    assert len(rest) == 15 and first.text == RELEVANT and "SLIP_IN_CARRY" in first.context
    assert lessons.parse("- free text lesson")[0].text == "free text lesson"


def test_select_puts_the_matching_class_first_even_when_old():
    picks = lessons.select(lessons.parse(LESSONS), TASK, classified_attempt(load_config()))
    assert picks[0].lesson.text == RELEVANT
    assert picks[0].why[:3] == ["same failure class SLIP_IN_CARRY", "same skill stack", "same phase carry"]
    assert len(picks) == lessons.MAX_PICKS and not any(p.relevant for p in picks[1:])
    assert [p.lesson.index for p in picks[1:]] == sorted((p.lesson.index for p in picks[1:]), reverse=True)


def test_select_matches_words_without_a_classification():
    text = "- 2026-10-02 (put the ball in the bowl; skill put_in): Slow the carry so a held cup is not lost.\n"
    (pick,) = lessons.select(lessons.parse(text), TASK, None)
    assert pick.relevant and pick.why == ["shared words: cup"]


def test_lesson_context_carries_class_and_phase():
    ctx = lessons.lesson_context(TASK, classified_attempt(load_config()))
    assert ctx == "stack the blue cup on the red cup; skill stack; SLIP_IN_CARRY in carry"
    assert lessons.lesson_context(TASK) == "stack the blue cup on the red cup; skill stack"


# ---- lessons in action, end to end ------------------------------------------------------------
def test_the_relevant_old_lesson_reaches_the_repair_and_is_shown(tmp_path):
    cfg = tmp_cfg(tmp_path)
    Path(cfg.repair.lessons_file).write_text(LESSONS)
    author, bus = ScriptedAuthor([V1], [V2]), NullBus()
    loop, _ = make_loop(cfg, author, bus)
    assert loop.run(TASK).ok

    report = author.reports[0]
    assert RELEVANT in report  # the "latest 12" rule alone would have dropped it
    assert "_(chosen: same failure class SLIP_IN_CARRY; same skill stack; same phase carry" in report
    (started,) = events(bus, "repair.started")
    assert started["lessons_total"] == 16 and started["lessons_relevant"] == 1
    assert started["lessons"][0]["lesson"] == RELEVANT and len(started["lessons"]) == lessons.MAX_PICKS
    assert started["lessons"][1]["why"] == ["recent"]


def test_no_lessons_key_after_a_static_rejection(tmp_path):
    cfg = tmp_cfg(tmp_path)
    author, bus = ScriptedAuthor(["import os\n" + V2], [V2]), NullBus()
    loop, _ = make_loop(cfg, author, bus, classify=False)
    assert loop.run(TASK).ok
    (started,) = events(bus, "repair.started")
    assert "lessons" not in started  # a static report carries no lessons


def test_droid_stores_the_lesson_with_its_failure_class(tmp_path, monkeypatch):
    droid = fake.install(monkeypatch)
    monkeypatch.setattr(author_mod.shutil, "which", lambda name: f"/opt/bin/{name}")
    cfg = tmp_cfg(tmp_path)
    droid.turns = [fake.Turn(skill=V2, output=fake.output(RELEVANT))]
    author = DroidSDKAuthor(cfg, NullBus(), lambda s: make_env("fake", cfg, s))
    attempt = classified_attempt(cfg)
    try:
        author.repair(TASK, V1, "FAIL", AuthorContext("run-1", 1, attempt.scene_before, None, {}, attempt=attempt))
    finally:
        author.close()
    (stored,) = lessons.parse(Path(cfg.repair.lessons_file).read_text())
    assert stored.text == RELEVANT and "SLIP_IN_CARRY in carry" in stored.context and "skill stack" in stored.context


# ---- audit trail ---------------------------------------------------------------------------------
def test_each_saved_version_gets_an_audit_record(tmp_path, monkeypatch):
    droid = fake.install(monkeypatch)
    monkeypatch.setattr(author_mod.shutil, "which", lambda name: f"/opt/bin/{name}")
    cfg = tmp_cfg(tmp_path)
    Path(cfg.repair.lessons_file).write_text(LESSONS)
    droid.turns = [fake.Turn(skill=V1, output=fake.output()),
                   fake.Turn(skill=V2, output=fake.output("Grip cups at mid-height.", change_type="strategy"))]
    bus = NullBus()
    author = DroidSDKAuthor(cfg, bus, lambda s: make_env("fake", cfg, s))
    try:
        loop, library = make_loop(cfg, author, bus)
        assert loop.run(TASK).ok
    finally:
        author.close()

    (saved,) = events(bus, "skill.saved")
    assert saved["commit"] is None  # fake target + audit_commit "auto": no git commit
    md = Path(saved["audit"])
    assert md == library.dir.parent / "audit" / "stack" / f"v{saved['version']}.md" and md.is_file()
    record = json.loads(md.with_suffix(".json").read_text())
    (rnd,) = record["rounds"]
    assert rnd["failure_class"] == "SLIP_IN_CARRY" and rnd["phase"] == "carry"
    assert rnd["trigger"].startswith("v0 on fake: SLIP_IN_CARRY") and rnd["lessons"][0] == RELEVANT
    assert rnd["diagnosis"] == "rim grip slipped" and rnd["change"] == "grip lower"
    assert rnd["change_type"] == "strategy" and rnd["lesson"] == "Grip cups at mid-height."
    assert record["approver"] == "AutoApprover" and record["approval_note"] == "auto-approved (non-real env)"
    assert record["sim"].startswith("passed on fake") and record["author"] == "DroidSDKAuthor"
    assert "+++ skill.py (after)" in record["diff"]
    text = md.read_text()
    assert "Droid's diagnosis: rim grip slipped" in text and "Approved by: AutoApprover" in text


def test_library_runs_are_not_audited(tmp_path):
    cfg = tmp_cfg(tmp_path)
    lib = temp_library()
    (lib / "stack.py").write_text(V2)
    bus = NullBus()
    loop, _ = make_loop(cfg, ScriptedAuthor([], []), bus, library_dir=lib)
    assert loop.run(TASK).ok
    (saved,) = events(bus, "skill.saved")
    assert "audit" not in saved and not (lib.parent / "audit").exists()


@pytest.mark.parametrize("mode,env,want", [("auto", "fake", False), ("auto", "real", True), ("auto", "sim", True),
                                           (True, "fake", True), ("false", "real", False), (False, "real", False)])
def test_commit_mode(mode, env, want):
    assert audit.should_commit(mode, env) is want


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_saved_skill_is_committed_without_touching_other_staged_work(tmp_path):
    repo = tmp_path / "repo"
    (repo / "skills").mkdir(parents=True)
    shutil.copytree(temp_library(), repo / "skills" / "library")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
    (repo / "wip.txt").write_text("someone's unrelated staged work\n")
    _git(repo, "add", "wip.txt")
    cfg = tmp_cfg(tmp_path, audit_commit=True, lessons_file=str(repo / "skills" / "lessons.md"))
    Path(cfg.repair.lessons_file).write_text(LESSONS)

    bus = NullBus()
    loop, _ = make_loop(cfg, ScriptedAuthor([V1], [V2]), bus, library_dir=repo / "skills" / "library")
    assert loop.run(TASK).ok
    (saved,) = events(bus, "skill.saved")
    assert saved["commit"] == _git(repo, "rev-parse", "HEAD").strip()
    message = _git(repo, "log", "-1", "--format=%B")
    assert message.startswith(f"skillsmith: save stack v{saved['version']} (after 1 repair round, run ")
    assert "Failure class: SLIP_IN_CARRY in carry" in message and RELEVANT in message
    assert "Approved by: AutoApprover" in message
    files = set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())
    assert files == {"skills/library/stack.py", "skills/registry.json", "skills/lessons.md",
                     "skills/audit/stack/v1.json", "skills/audit/stack/v1.md"}
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["wip.txt"]  # still staged, not committed
    assert not events(bus, "error")


def test_audit_failure_never_fails_the_run(tmp_path, monkeypatch):
    cfg = tmp_cfg(tmp_path, audit_commit=True)
    monkeypatch.setattr(audit, "commit", lambda paths, record: (_ for _ in ()).throw(RuntimeError("git broke")))
    bus = NullBus()
    loop, _ = make_loop(cfg, ScriptedAuthor([V1], [V2]), bus)
    assert loop.run(TASK).ok
    (saved,) = events(bus, "skill.saved")
    assert saved["commit"] is None and Path(saved["audit"]).is_file()
    assert any(e["where"] == "repair.audit" and "git broke" in e["message"] for e in events(bus, "error"))
