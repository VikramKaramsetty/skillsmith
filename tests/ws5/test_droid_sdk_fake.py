"""WS5.2: DroidSDKAuthor plumbing against the fake droid_sdk (runs in `make check`, no SDK needed).

Covers draft, repair, session reuse across rounds, refused actions, timeout, lessons, images and
event visibility. The real-SDK version of the end-to-end test is test_droid_sdk_e2e.py.
"""

import copy

import fake_droid_sdk as fake
import pytest

from skillsmith.config import Config, load_config
from skillsmith.demo import demo_sources, temp_library
from skillsmith.events import NullBus
from skillsmith.hal.base import Env, make_env
from skillsmith.hal.fake import FakeWorld
from skillsmith.repair import droid_sdk as author_mod
from skillsmith.repair.approvers import AutoApprover
from skillsmith.repair.droid_sdk import DroidSDKAuthor
from skillsmith.repair.droid_tools import MCP_SERVER_NAME, REPAIR_RESULT_SCHEMA, TOOL_NAMES
from skillsmith.repair.loop import RepairLoop
from skillsmith.repair.protocols import AuthorContext
from skillsmith.skills.library import SkillLibrary
from skillsmith.tasks import parse_task

TASK_TEXT = "stack the blue cup on the red cup"
TASK = parse_task(TASK_TEXT)
LESSON = "Grip cups at mid-height; rim grips slip in carry."
V1, V2 = demo_sources()


def tmp_cfg(tmp_path, **droid) -> Config:
    data = copy.deepcopy(load_config().raw())
    data["repair"]["droid_ws_root"] = str(tmp_path / "droid_ws")
    data["repair"]["lessons_file"] = str(tmp_path / "lessons.md")
    data["droid"].update(droid)
    return Config(data)


@pytest.fixture
def droid(monkeypatch):
    controller = fake.install(monkeypatch)
    monkeypatch.setattr(author_mod.shutil, "which", lambda name: f"/opt/bin/{name}")
    return controller


def make_author(cfg, bus):
    return DroidSDKAuthor(cfg, bus, lambda s: make_env("fake", cfg, s))


def ctx(cfg, rnd, run_id="run-1", snapshot=None):
    library = SkillLibrary(temp_library()).sources()  # stack v2 calls pick_place
    return AuthorContext(run_id, rnd, FakeWorld.demo(cfg).observe(), snapshot, library)


def events(bus, kind):
    return [e["data"] for e in bus.events if e["type"] == kind]


def run_loop(cfg, author, bus):
    w = FakeWorld.demo(cfg, rim_slip=True)
    library = SkillLibrary(temp_library())
    loop = RepairLoop(cfg, Env("fake", w, w, w), lambda s: make_env("fake", cfg, s), library,
                      author, AutoApprover("fake"), bus)
    return loop.run(TASK), library


def test_draft_then_repair_reuses_one_session(tmp_path, droid):
    cfg = tmp_cfg(tmp_path)
    droid.turns = [fake.Turn(skill=V1, output=fake.output()), fake.Turn(skill=V2, output=fake.output(LESSON))]
    bus = NullBus()
    author = make_author(cfg, bus)
    try:
        result, library = run_loop(cfg, author, bus)
    finally:
        author.close()

    assert result.ok and result.rounds == 1 and library.has("stack")
    assert len(droid.sessions) == 1
    session = droid.sessions[0]
    assert session.opened and session.closed
    assert len(session.prompts) == 2
    draft_prompt, repair_prompt = (p for p, _ in session.prompts)
    assert "Read AGENTS.md" in draft_prompt and "lost blue cup" in repair_prompt
    assert (author.workspace(bus.run_id) / "skill.py").read_text() == V2


def test_session_is_configured_with_tight_controls(tmp_path, droid):
    cfg = tmp_cfg(tmp_path, model="fast-model", reasoning="high")
    droid.turns = [fake.Turn(skill=V2, output=fake.output())]
    author = make_author(cfg, NullBus())
    try:
        author.repair(TASK, V1, "FAIL: lost blue cup", ctx(cfg, 1))
    finally:
        author.close()

    s = droid.sessions[0]
    assert s.model == "fast-model" and s.reasoning_effort is fake.ReasoningEffort.HIGH
    assert str(s.runtime.executable) == "/opt/bin/droid"
    assert s.config.autonomy is fake.Autonomy.LOW and s.config.disabled_tools == ["Execute"]
    assert s.config.restrict_tools is None
    assert s.config.system_prompt["append"] == author_mod.SYSTEM_APPEND
    (server,) = s.config.mcp_servers
    assert server.name == MCP_SERVER_NAME and tuple(t.name for t in server.tools) == TOOL_NAMES
    _, kwargs = s.prompts[0]
    assert kwargs["output"].schema == REPAIR_RESULT_SCHEMA and kwargs["timeout"] == cfg.droid.timeout_s
    assert "images" not in kwargs  # no snapshot this round


def test_every_step_is_an_event(tmp_path, droid):
    cfg = tmp_cfg(tmp_path)
    droid.turns = [fake.Turn(skill=V2, output=fake.output(LESSON), error_event="tool crashed")]
    bus = NullBus()
    author = make_author(cfg, bus)
    try:
        author.repair(TASK, V1, "FAIL: lost blue cup", ctx(cfg, 1))
    finally:
        author.close()

    started = events(bus, "droid.started")[0]
    assert started["via"] == "droid-sdk" and started["autonomy"] == "low" and started["round"] == 1
    assert events(bus, "droid.tools")[0]["tools"] == ["Read", "Edit"]
    assert events(bus, "droid.message")[0]["text"].startswith("Plan:")
    calls = events(bus, "droid.tool_call")
    assert [c["name"] for c in calls] == ["skillsmith___validate_skill", "skillsmith___simulate_skill"]
    assert "mappingproxy" not in calls[0]["input"] and '"check"' in calls[0]["input"]
    assert all("PASS" in r["content"] for r in events(bus, "droid.tool_result"))
    assert [c["ok"] for c in events(bus, "droid.check")] == [True, True]
    assert events(bus, "droid.permission") == [
        {"round": 1, "action": "EditAction", "target": str(author.workspace("run-1") / "skill.py"),
         "allowed": True, "reason": "edit to skill.py"}]
    (res,) = events(bus, "droid.result")
    assert res["result"] == dict(fake.output(LESSON)) and isinstance(res["result"], dict)
    assert res["subtype"] == "success" and res["duration"] == 12.5 and res["credits"] == 0.5
    assert res["session_id"] == "sess-1" and res["error"] is None
    assert {"where": "droid", "message": "tool crashed"} in events(bus, "error")


def test_refused_actions_never_take_effect(tmp_path, droid):
    cfg = tmp_cfg(tmp_path)
    droid.turns = [fake.Turn(skill="import os\n", edit_path="AGENTS.md", commands=["pytest"],
                             questions=["Which grip height?"], tools=[], output=fake.output())]
    bus = NullBus()
    author = make_author(cfg, bus)
    ws = author.workspace("run-1")
    try:
        new = author.repair(TASK, V1, "FAIL: lost blue cup", ctx(cfg, 1))
    finally:
        author.close()

    assert new == V1 and "import os" not in (ws / "AGENTS.md").read_text()
    perms = events(bus, "droid.permission")
    assert [(p["action"], p["allowed"]) for p in perms] == [
        ("EditAction", False), ("ExecuteAction", False), ("AskUser", False)]
    assert "only skill.py" in perms[0]["reason"] and "simulate_skill" in perms[1]["reason"]
    edit, shell, question = droid.sessions[0].responses
    for reply in (edit, shell):
        assert reply.selected_option is fake.ToolConfirmationOutcome.CANCEL
        assert reply.comment.startswith("Refused by Skillsmith policy")
    assert question.cancelled


def test_timeout_drops_the_session_and_next_round_opens_a_fresh_one(tmp_path, droid):
    cfg = tmp_cfg(tmp_path)
    droid.turns = [fake.Turn(raises=fake.RunTimeoutError("turn timed out")),
                   fake.Turn(skill=V2, output=fake.output())]
    bus = NullBus()
    author = make_author(cfg, bus)
    try:
        with pytest.raises(fake.RunTimeoutError):
            author.repair(TASK, V1, "FAIL: lost blue cup", ctx(cfg, 1))
        assert author._sessions == {} and droid.sessions[0].closed
        assert author.repair(TASK, V1, "FAIL: lost blue cup", ctx(cfg, 2)) == V2
    finally:
        author.close()

    assert len(droid.sessions) == 2 and droid.sessions[1].closed
    err = events(bus, "error")[0]
    assert err["where"] == "droid.round" and err["round"] == 1 and "timed out" in err["message"]


def test_unsuccessful_turn_without_an_edit_is_an_error(tmp_path, droid):
    cfg = tmp_cfg(tmp_path)
    droid.turns = [fake.Turn(subtype="error_max_turns", tools=[])]
    bus = NullBus()
    author = make_author(cfg, bus)
    try:
        with pytest.raises(RuntimeError, match="error_max_turns"):
            author.repair(TASK, V1, "FAIL: lost blue cup", ctx(cfg, 1))
    finally:
        author.close()
    assert any("without changing skill.py" in e["message"] for e in events(bus, "error"))
    assert events(bus, "droid.result")[0]["result"] is None


def test_lessons_are_saved_once_and_reach_the_next_workspace(tmp_path, droid):
    cfg = tmp_cfg(tmp_path)
    droid.turns = [fake.Turn(skill=V2, output=fake.output(LESSON)),
                   fake.Turn(skill=V2, output=fake.output(LESSON)),
                   fake.Turn(skill=V2, output=fake.output(""))]
    bus = NullBus()
    author = make_author(cfg, bus)
    try:
        author.repair(TASK, V1, "FAIL", ctx(cfg, 1, run_id="run-a"))
        author.repair(TASK, V1, "FAIL", ctx(cfg, 1, run_id="run-b"))
        author.repair(TASK, V1, "FAIL", ctx(cfg, 1, run_id="run-c"))
    finally:
        author.close()

    assert len(droid.sessions) == 3  # one session per run
    assert (tmp_path / "lessons.md").read_text().count(LESSON) == 1
    assert [e["lesson"] for e in events(bus, "droid.lesson")] == [LESSON]
    assert LESSON in (author.workspace("run-b") / "LESSONS.md").read_text()


def test_failure_snapshot_rides_along_as_an_image(tmp_path, droid):
    snap = tmp_path / "snap.jpg"
    snap.write_bytes(b"\xff\xd8jpeg")
    droid.turns = [fake.Turn(skill=V2, output=fake.output()), fake.Turn(skill=V2, output=fake.output())]
    for attach, run_id in ((True, "run-on"), (False, "run-off")):
        cfg = tmp_cfg(tmp_path, attach_snapshot=attach)
        author = make_author(cfg, NullBus())
        try:
            author.repair(TASK, V1, "FAIL", ctx(cfg, 1, run_id=run_id, snapshot=str(snap)))
        finally:
            author.close()

    (image,) = droid.sessions[0].prompts[0][1]["images"]
    assert image.source.media_type == "image/jpeg" and image.source.data == "/9hqcGVn"
    assert "images" not in droid.sessions[1].prompts[0][1]
