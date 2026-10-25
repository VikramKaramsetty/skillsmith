"""A fake `droid_sdk` package for CI: no droid binary, no login, no network.

It mirrors only the slice of droid-sdk 0.5 that `skillsmith.repair.droid_sdk` touches (same
class names and fields), so `DroidSDKAuthor` runs unchanged. `install(monkeypatch)` puts it in
`sys.modules` as `droid_sdk` and `droid_sdk.mcp` (shadowing the real SDK if present) and returns
a `FakeDroid` controller.

Each `stream()` call plays the next scripted `Turn`: a message, a skill.py edit and shell
commands routed through the author's real permission handler (the file is written only if
allowed), questions, calls to the real `skillsmith` MCP tools, then a result or an exception.
"""

from __future__ import annotations

import enum
import sys
import types
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, List, Optional


# ---- enums and config -------------------------------------------------------------------------
class Autonomy(enum.Enum):
    OFF = "off"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReasoningEffort(enum.Enum):
    OFF = "off"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolConfirmationOutcome(enum.Enum):
    PROCEED_ONCE = "proceed_once"
    PROCEED_ALWAYS = "proceed_always"
    CANCEL = "cancel"


@dataclass
class JsonSchema:
    schema: dict


@dataclass
class Runtime:
    executable: str


@dataclass
class SessionConfig:
    autonomy: Autonomy
    mcp_servers: list = field(default_factory=list)
    disabled_tools: Optional[list] = None
    restrict_tools: Optional[list] = None
    system_prompt: Any = None


@dataclass
class InteractionHandlers:
    on_permission: Callable
    on_question: Callable


@dataclass
class Base64ImageSource:
    data: str
    media_type: str


@dataclass
class Image:
    source: Base64ImageSource


# ---- permission and question requests -----------------------------------------------------------
@dataclass
class EditAction:
    file_path: str


@dataclass
class ExecuteAction:
    full_command: str


@dataclass
class PermissionOption:
    label: str
    value: ToolConfirmationOutcome


@dataclass
class PermissionResponse:
    selected_option: ToolConfirmationOutcome
    comment: Optional[str] = None


@dataclass
class PermissionRequest:
    actions: list
    options: list

    def respond(self, outcome: ToolConfirmationOutcome, *, comment: Optional[str] = None) -> PermissionResponse:
        return PermissionResponse(outcome, comment)


@dataclass
class Question:
    question: str


@dataclass
class QuestionResponse:
    cancelled: bool


@dataclass
class QuestionRequest:
    questions: list

    def cancel(self) -> QuestionResponse:
        return QuestionResponse(cancelled=True)


# ---- stream events and results -------------------------------------------------------------------
@dataclass
class AssistantMessage:
    text: str


@dataclass
class ToolCall:
    name: str
    input: Any


@dataclass
class ToolResult:
    tool_name: str
    content: Any
    is_error: bool = False


@dataclass
class ErrorEvent:
    message: str


@dataclass
class Usage:
    factory_credits: Optional[float] = None


@dataclass
class RunResult:
    subtype: str = "success"
    text: str = ""
    structured_output: Any = None
    usage: Optional[Usage] = None
    duration: Optional[timedelta] = None
    session_id: Optional[str] = None
    error: Any = None


class RunTimeoutError(Exception):
    pass


# ---- MCP ------------------------------------------------------------------------------------------
@dataclass
class SdkMcpTool:
    name: str
    description: str
    fn: Callable

    async def handler(self, args: dict) -> str:
        value = self.fn(**args)
        if not isinstance(value, str):  # droid-sdk 0.5 mcp.tool(): "must return str or ToolResponse"
            raise TypeError("MCP tool handlers must return str or ToolResponse")
        return value


def mcp_content(text: str) -> list:
    """What droid-sdk 0.5's MCP server sends the model for a tool result: one text block, never an image."""
    return [{"type": "text", "text": text}]


@dataclass
class SdkMcpServer:
    name: str
    tools: list


def tool(name: str, description: str, fn: Callable) -> SdkMcpTool:
    return SdkMcpTool(name, description, fn)


def create_sdk_mcp_server(name: str, tools: list) -> SdkMcpServer:
    return SdkMcpServer(name, list(tools))


# ---- the script -----------------------------------------------------------------------------------
@dataclass
class Turn:
    """What Droid does in one stream() call. `skill` is the new skill.py (None = no edit)."""

    skill: Optional[str] = None
    message: str = "Plan: smallest change, then simulate."
    edit_path: str = "skill.py"  # where Droid tries to write `skill`, relative to the workspace
    commands: List[str] = field(default_factory=list)  # shell commands Droid asks to run
    questions: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=lambda: ["validate_skill", "simulate_skill"])
    tool_args: dict = field(default_factory=dict)  # tool name -> arguments Droid passes
    error_event: Optional[str] = None
    output: Any = None
    subtype: str = "success"
    raises: Optional[Exception] = None


@dataclass
class FakeDroid:
    turns: List[Turn] = field(default_factory=list)
    sessions: List[Session] = field(default_factory=list)
    listed_tools: List[str] = field(default_factory=lambda: ["Read", "Edit"])


def output(lesson: str = "", **overrides) -> types.MappingProxyType:
    """A structured result shaped like REPAIR_RESULT_SCHEMA, read-only like the real SDK's."""
    out = {"diagnosis": "rim grip slipped", "change": "grip lower", "change_type": "parameter",
           "sim_checked": True, "confidence": 0.8, "lesson": lesson}
    out.update(overrides)
    return types.MappingProxyType(out)


CONTROLLER = FakeDroid()


class Session:
    def __init__(self, *, cwd, model=None, reasoning_effort=None, config=None, interactions=None, runtime=None):
        self.cwd = Path(cwd)
        self.model, self.reasoning_effort = model, reasoning_effort
        self.config, self.interactions, self.runtime = config, interactions, runtime
        self.prompts: List[tuple] = []
        self.tool_results: List[tuple] = []  # (tool name, MCP content) exactly as the model receives it
        self.responses: list = []  # every permission/question reply the author gave
        self.opened = self.closed = False
        self.settings_updates: List[tuple] = []  # (model, reasoning_effort) per update_settings call
        CONTROLLER.sessions.append(self)

    async def update_settings(self, *, model=None, reasoning_effort=None, **_):
        self.settings_updates.append((model, reasoning_effort))
        self.model, self.reasoning_effort = model or self.model, reasoning_effort or self.reasoning_effort

    async def __aenter__(self):
        self.opened = True
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def list_tools(self):
        return [types.SimpleNamespace(id=t) for t in CONTROLLER.listed_tools]

    def stream(self, prompt: str, **kwargs) -> Stream:
        self.prompts.append((prompt, kwargs))
        return Stream(self, CONTROLLER.turns.pop(0))


class Stream:
    def __init__(self, session: Session, turn: Turn):
        self.session, self.turn = session, turn
        self.result: Optional[RunResult] = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._play()

    def _ask(self, action) -> bool:
        options = [PermissionOption(o.value, o)
                   for o in (ToolConfirmationOutcome.PROCEED_ONCE, ToolConfirmationOutcome.CANCEL)]
        reply = self.session.interactions.on_permission(PermissionRequest([action], options))
        self.session.responses.append(reply)
        return reply.selected_option is ToolConfirmationOutcome.PROCEED_ONCE

    async def _play(self):
        t, s = self.turn, self.session
        if t.raises is not None:
            raise t.raises
        yield AssistantMessage(t.message)
        if t.skill is not None:
            path = s.cwd / t.edit_path
            if self._ask(EditAction(str(path))):
                path.write_text(t.skill)
        for command in t.commands:
            self._ask(ExecuteAction(command))
        for q in t.questions:
            s.responses.append(s.interactions.on_question(QuestionRequest([Question(q)])))
        handlers = {x.name: x for server in s.config.mcp_servers for x in server.tools}
        for name in t.tools:
            yield ToolCall(f"skillsmith___{name}", types.MappingProxyType({"why": ("check",)}))
            content = mcp_content(await handlers[name].handler(t.tool_args.get(name, {})))
            s.tool_results.append((name, content))
            yield ToolResult(name, content)
        if t.error_event:
            yield ErrorEvent(t.error_event)
        self.result = RunResult(subtype=t.subtype, text="done", structured_output=t.output,
                                usage=Usage(0.5), duration=timedelta(seconds=12.5), session_id="sess-1")


def install(monkeypatch) -> FakeDroid:
    """Register this module as `droid_sdk` (and `droid_sdk.mcp`) for one test."""
    global CONTROLLER
    CONTROLLER = FakeDroid()
    me = sys.modules[__name__]
    monkeypatch.setitem(sys.modules, "droid_sdk", me)
    monkeypatch.setitem(sys.modules, "droid_sdk.mcp", me)
    return CONTROLLER
