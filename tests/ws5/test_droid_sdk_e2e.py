"""WS5.1: DroidSDKAuthor end to end on the fake env, against the real droid-sdk 0.5 classes.

Only `droid_sdk.Session` is replaced (CI has no droid binary or login). Everything Droid would
send back is a real SDK object: AssistantMessage, ToolCall/ToolResult, PermissionRequest with
EditAction/ExecuteAction, QuestionRequest, the real SDK MCP tools built from RepairTools, and a
RunSuccess whose structured output is the SDK's read-only MappingProxyType.
"""

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sdk = pytest.importorskip("droid_sdk")
pytest.importorskip("droid_sdk.mcp")  # the [mcp] extra

from skillsmith.config import Config, load_config  # noqa: E402
from skillsmith.demo import demo_sources, temp_library  # noqa: E402
from skillsmith.events import NullBus  # noqa: E402
from skillsmith.hal.base import Env, make_env  # noqa: E402
from skillsmith.hal.fake import FakeWorld  # noqa: E402
from skillsmith.repair import droid_sdk as author_mod  # noqa: E402
from skillsmith.repair.approvers import AutoApprover  # noqa: E402
from skillsmith.repair.droid_sdk import DroidSDKAuthor  # noqa: E402
from skillsmith.repair.loop import RepairLoop  # noqa: E402
from skillsmith.skills.library import SkillLibrary  # noqa: E402
from skillsmith.tasks import parse_task  # noqa: E402

TASK = "stack the blue cup on the red cup"
LESSON = "Grip cups at mid-height; rim grips slip in carry."
NOW = datetime.now(timezone.utc)


def tmp_cfg(tmp_path) -> Config:
    data = copy.deepcopy(load_config().raw())
    data["repair"]["droid_ws_root"] = str(tmp_path / "droid_ws")
    data["repair"]["lessons_file"] = str(tmp_path / "lessons.md")
    return Config(data)


def _use(name, **inp):
    return sdk.ToolUseBlock(id=f"tu_{name}", name=name, input=inp)


def _options():
    return [sdk.PermissionOption(label=o.value, value=o)
            for o in (sdk.ToolConfirmationOutcome.PROCEED_ONCE, sdk.ToolConfirmationOutcome.CANCEL)]


class FakeStream:
    """Plays one Droid turn using real SDK objects and the session's real handlers."""

    def __init__(self, session, new_source, fail_with=None):
        self.session, self.new_source, self.fail_with = session, new_source, fail_with
        self.result = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        if self.fail_with is not None:
            raise self.fail_with
        s = self.session
        ws = s.kwargs["cwd"]
        on_permission = s.kwargs["interactions"].on_permission
        on_question = s.kwargs["interactions"].on_question
        yield sdk.AssistantMessage(id="m1", content=(), text="Plan: grip lower, then simulate.",
                                   created_at=NOW, updated_at=NOW)

        edit = sdk.EditAction(tool_use=_use("Edit"), file_path=str(ws / "skill.py"), file_name="skill.py")
        reply = on_permission(sdk.PermissionRequest(actions=[edit], options=_options()))
        if reply.selected_option is sdk.ToolConfirmationOutcome.PROCEED_ONCE:
            (ws / "skill.py").write_text(self.new_source)

        shell = sdk.ExecuteAction(tool_use=_use("Execute"), full_command="pytest", command="pytest")
        s.refusals.append(on_permission(sdk.PermissionRequest(actions=[shell], options=_options())))
        s.refusals.append(on_question(sdk.QuestionRequest(
            tool_call_id="q1", questions=[sdk.Question(index=0, topic="grip", question="Which height?")])))

        tools = {t.name: t for t in s.kwargs["config"].mcp_servers[0].tools}
        for name in ("validate_skill", "simulate_skill"):
            yield sdk.ToolCall(name=f"skillsmith___{name}", tool_use_id=f"tu_{name}", input={"why": ["check"]})
            out = await tools[name].handler({})
            yield sdk.ToolResult(tool_use_id=f"tu_{name}", tool_name=name, content=[{"text": out}],
                                 is_error=False)

        self.result = sdk.RunSuccess(
            text="done", messages=(), usage=sdk.Usage(10, 20, 0, 0, 0, factory_credits=0.5),
            duration=timedelta(seconds=12.5), turn_count=3, session_id="sess-1",
            structured_output={"diagnosis": "rim grip slipped", "change": "grip lower",
                               "change_type": "parameter", "sim_checked": True, "confidence": 0.8, "lesson": LESSON})


class FakeSession:
    """Stands in for droid_sdk.Session; records how DroidSDKAuthor configured it."""

    opened = []
    script = []  # per-turn (new_source, exception)

    def __init__(self, **kwargs):
        self.kwargs, self.prompts, self.refusals, self.closed = kwargs, [], [], False
        FakeSession.opened.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def list_tools(self):
        return [SimpleNamespace(id="Read"), SimpleNamespace(id="Edit")]

    def stream(self, prompt, **kwargs):
        self.prompts.append((prompt, kwargs))
        new_source, error = FakeSession.script.pop(0)
        return FakeStream(self, new_source, error)


@pytest.fixture
def fake_sdk(monkeypatch):
    FakeSession.opened, FakeSession.script = [], []
    monkeypatch.setattr(sdk, "Session", FakeSession)
    monkeypatch.setattr(author_mod.shutil, "which", lambda name: f"/opt/bin/{name}")
    return FakeSession


def run_loop(cfg, author, bus):
    w = FakeWorld.demo(cfg, rim_slip=True)
    library = SkillLibrary(temp_library())
    loop = RepairLoop(cfg, Env("fake", w, w, w), lambda s: make_env("fake", cfg, s), library,
                      author, AutoApprover("fake"), bus)
    return loop.run(parse_task(TASK)), library


def test_droid_drafts_then_repairs_rim_slip_on_fake_env(tmp_path, fake_sdk):
    cfg = tmp_cfg(tmp_path)
    v1, v2 = demo_sources()
    fake_sdk.script = [(v1, None), (v2, None)]  # draft = block-tuned v1, repair = v2
    bus = NullBus()
    author = DroidSDKAuthor(cfg, bus, lambda s: make_env("fake", cfg, s))
    try:
        result, library = run_loop(cfg, author, bus)
    finally:
        author.close()

    assert result.ok and result.rounds == 1 and library.has("stack")
    assert len(fake_sdk.opened) == 1 and fake_sdk.opened[0].closed  # one session reused, then closed
    session = fake_sdk.opened[0]
    assert len(session.prompts) == 2 and "lost blue cup" in session.prompts[1][0]
    assert str(session.kwargs["runtime"].executable) == "/opt/bin/droid"
    assert session.kwargs["config"].autonomy == sdk.Autonomy.HIGH

    ev = lambda t: [e["data"] for e in bus.events if e["type"] == t]  # noqa: E731
    results = ev("droid.result")
    assert results[-1]["result"]["lesson"] == LESSON  # MappingProxyType output is kept
    assert results[-1]["duration"] == 12.5 and results[-1]["credits"] == 0.5
    assert ev("droid.lesson") and LESSON in (tmp_path / "lessons.md").read_text()
    calls = ev("droid.tool_call")
    assert calls and "mappingproxy" not in calls[0]["input"] and '"check"' in calls[0]["input"]
    assert "PASS" in ev("droid.tool_result")[0]["content"]
    assert [c["ok"] for c in ev("droid.check")][-2:] == [True, True]
    perms = ev("droid.permission")
    assert {(p["action"], p["allowed"]) for p in perms} == {("EditAction", True), ("ExecuteAction", False),
                                                            ("AskUser", False)}
    assert ev("droid.tools")[0]["tools"] == ["Read", "Edit"]
    refused_shell, question = session.refusals[:2]
    assert refused_shell.selected_option is sdk.ToolConfirmationOutcome.CANCEL
    assert "Refused by Skillsmith policy" in refused_shell.comment
    assert question.cancelled


def test_droid_failure_emits_event_and_drops_session(tmp_path, fake_sdk):
    cfg = tmp_cfg(tmp_path)
    fake_sdk.script = [(None, sdk.RunTimeoutError("turn timed out"))]
    bus = NullBus()
    author = DroidSDKAuthor(cfg, bus, lambda s: make_env("fake", cfg, s))
    try:
        with pytest.raises(sdk.RunTimeoutError):
            run_loop(cfg, author, bus)
        assert author._sessions == {} and fake_sdk.opened[0].closed
    finally:
        author.close()
    errors = [e["data"] for e in bus.events if e["type"] == "error"]
    assert errors and errors[0]["where"] == "droid.round" and "timed out" in errors[0]["message"]


def test_missing_droid_cli_fails_with_install_hint(tmp_path, monkeypatch):
    cfg = tmp_cfg(tmp_path)
    monkeypatch.setattr(author_mod.shutil, "which", lambda name: None)
    bus = NullBus()
    author = DroidSDKAuthor(cfg, bus, lambda s: make_env("fake", cfg, s))
    try:
        with pytest.raises(RuntimeError, match="not found on PATH"):
            run_loop(cfg, author, bus)
    finally:
        author.close()
    assert any(e["type"] == "error" and "FACTORY_API_KEY" in e["data"]["message"] for e in bus.events)
