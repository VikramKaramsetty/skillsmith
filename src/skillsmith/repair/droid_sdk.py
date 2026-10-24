"""DroidSDKAuthor: Factory Droid working INSIDE the product, through the Droid Python SDK.

This is the product path (docs/NORTH_STAR.md). The CLI author in droid_client.py is only a
stage-emergency fallback.

- **Trigger:** RepairLoop calls draft()/repair() when the robot's attempt fails the camera
  check, a gate rejects Droid's code, a human rejects with a note, or a task is new.
- **Control:** autonomy LOW, shell execution disabled, a permission handler (droid_tools.decide)
  that allows only edits to skill.py in the workspace and calls to our `skillsmith` tools.
  After Droid returns we re-run static, sim and safety ourselves.
- **Visibility:** every assistant message, tool call, tool result, permission decision and the
  structured result is emitted as an event for the dashboard and runs/<id>/events.jsonl.
- **Memory:** one SDK session per run stays open across repair rounds (kept on a private
  event loop), so Droid remembers what it already tried.

Requires the `droid` extra (`droid-sdk[mcp]`), the `droid` CLI on PATH, and a Droid login or
FACTORY_API_KEY. SDK reference: https://docs.factory.ai/sdk/python
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Callable, Dict, Optional

from skillsmith.events import EventBus
from skillsmith.repair.droid_client import SKILL_STUB
from skillsmith.repair.droid_tools import (
    MCP_SERVER_NAME,
    REPAIR_RESULT_SCHEMA,
    RepairTools,
    action_target,
    append_lesson,
    decide,
)
from skillsmith.repair.protocols import AuthorContext
from skillsmith.repair.report import draft_brief
from skillsmith.repair.workspace import lessons_path, prepare_workspace, workspace_dir
from skillsmith.types import Scene, TaskSpec

SYSTEM_APPEND = (
    "You write and repair robot skills for the Skillsmith arm. Your working directory holds "
    "AGENTS.md (rules), API.md (the only API a skill may use), LESSONS.md (what earlier repairs "
    "learned), library/ (saved skills) and skill.py, the only file you may edit. You cannot run "
    "shell commands. Use the skillsmith tools: validate_skill, then simulate_skill on your final "
    "version before you finish. Reply with the requested JSON."
)

PROMPT_HEADER = """\
Read AGENTS.md, API.md and LESSONS.md, then the evidence below. Fix skill.py with the smallest
change that addresses the failure, check it with validate_skill and simulate_skill, then reply
with the JSON result.

---

"""


class DroidSDKAuthor:
    def __init__(self, cfg, bus: EventBus, make_check_env: Callable[[Scene], object]):
        self.cfg = cfg
        self.bus = bus
        self.d = cfg.droid
        self.make_check_env = make_check_env
        self._loop = asyncio.new_event_loop()
        self._sessions: Dict[str, object] = {}
        self._tools: Dict[str, RepairTools] = {}
        self.last_result: Optional[dict] = None

    # ---- SkillAuthor -------------------------------------------------------------------
    def draft(self, task: TaskSpec, ctx: AuthorContext) -> str:
        params = {k: type(v).__name__ for k, v in task.params.items()}
        stub = SKILL_STUB.format(name=task.skill, params=params, args=", ".join(task.params))
        return self._round(task, stub, draft_brief(task, ctx.scene), ctx)

    def repair(self, task: TaskSpec, source: str, report_md: str, ctx: AuthorContext) -> str:
        return self._round(task, source, report_md, ctx)

    def close(self) -> None:
        for session in self._sessions.values():
            try:
                self._loop.run_until_complete(session.__aexit__(None, None, None))
            except Exception as e:
                self.bus.emit("error", where="droid.close", message=repr(e))
        self._sessions.clear()
        self._loop.close()

    # ---- one round -----------------------------------------------------------------------
    def _round(self, task: TaskSpec, source: str, report_md: str, ctx: AuthorContext) -> str:
        ws = prepare_workspace(self.cfg, source, PROMPT_HEADER + report_md, report_md, ctx)
        tools = self._tools.get(ctx.run_id)
        if tools is None:
            tools = RepairTools(self.cfg, self.bus, ws, task, ctx.library, self.make_check_env,
                                sim_budget=self.d.get("sim_tool_budget", 3))
            self._tools[ctx.run_id] = tools
        tools.update(ctx.scene, ctx.round, ctx.library)

        self.bus.emit("droid.started", round=ctx.round, via="droid-sdk", workspace=str(ws),
                      autonomy=self.d.autonomy, disabled_tools=list(self.d.disabled_tools or []))
        result = self._loop.run_until_complete(self._stream(ws, report_md, ctx, tools))
        new = (ws / "skill.py").read_text()
        if not _succeeded(result) and new == source:
            raise RuntimeError(f"Droid finished with '{getattr(result, 'subtype', '?')}' without changing skill.py")
        out = self.last_result or {}
        if out.get("lesson") and append_lesson(lessons_path(self.cfg), task.text, out["lesson"]):
            self.bus.emit("droid.lesson", round=ctx.round, lesson=out["lesson"])
        return new

    async def _stream(self, ws: Path, report_md: str, ctx: AuthorContext, tools: RepairTools):
        from droid_sdk import JsonSchema

        session = await self._session(ctx.run_id, ws, tools)
        kwargs = {"output": JsonSchema(REPAIR_RESULT_SCHEMA), "timeout": self.d.timeout_s}
        images = self._images(ctx.snapshot_path)
        if images:
            kwargs["images"] = images
        async with session.stream(PROMPT_HEADER + report_md, **kwargs) as stream:
            async for event in stream:
                self._emit(event, ctx.round)
        result = stream.result
        out = getattr(result, "output", None) or getattr(result, "structured_output", None)
        self.last_result = dict(out) if isinstance(out, dict) else None
        usage = getattr(result, "usage", None)
        self.bus.emit("droid.result", round=ctx.round, subtype=getattr(result, "subtype", None),
                      result=self.last_result, text=getattr(result, "text", ""),
                      session_id=getattr(result, "session_id", None),
                      duration=getattr(result, "duration", None),
                      credits=getattr(usage, "factory_credits", None) if usage else None)
        return result

    async def _session(self, run_id: str, ws: Path, tools: RepairTools):
        if run_id in self._sessions:
            return self._sessions[run_id]
        from droid_sdk import Session

        session = Session(cwd=ws, model=self.d.model or None, config=self.build_config(tools),
                          interactions=self.build_interactions(ws, tools))
        await session.__aenter__()
        self._sessions[run_id] = session
        try:  # show judges exactly which tools Droid has
            listed = await session.list_tools()
            self.bus.emit("droid.tools", tools=[getattr(t, "id", None) or getattr(t, "name", str(t)) for t in listed])
        except Exception as e:
            self.bus.emit("error", where="droid.list_tools", message=repr(e))
        return session

    # ---- control -------------------------------------------------------------------------
    def build_config(self, tools: RepairTools):
        from droid_sdk import Autonomy, SessionConfig
        from droid_sdk.mcp import create_sdk_mcp_server, tool

        server = create_sdk_mcp_server(name=MCP_SERVER_NAME,
                                       tools=[tool(name, desc, fn) for name, desc, fn in tools.functions()])
        return SessionConfig(
            autonomy=Autonomy[str(self.d.autonomy).upper()],
            mcp_servers=[server],
            disabled_tools=list(self.d.disabled_tools or []) or None,
            restrict_tools=list(self.d.restrict_tools or []) or None,
            system_prompt={"type": "preset", "preset": "droid", "append": SYSTEM_APPEND},
        )

    def build_interactions(self, ws: Path, tools: RepairTools):
        from droid_sdk import InteractionHandlers, ToolConfirmationOutcome

        def on_permission(request):
            decisions = [decide(action, ws) for action in request.actions]
            for action, (ok, why) in zip(request.actions, decisions):
                self.bus.emit("droid.permission", round=tools.round, action=type(action).__name__,
                              target=action_target(action), allowed=ok, reason=why)
            offered = {option.value for option in request.options}
            if all(ok for ok, _ in decisions) and ToolConfirmationOutcome.PROCEED_ONCE in offered:
                return request.respond(ToolConfirmationOutcome.PROCEED_ONCE)
            refused = "; ".join(why for ok, why in decisions if not ok) or "approval option not offered"
            return request.respond(ToolConfirmationOutcome.CANCEL, comment=f"Refused by Skillsmith policy: {refused}")

        return InteractionHandlers(on_permission=on_permission)

    # ---- visibility ----------------------------------------------------------------------
    def _emit(self, event, rnd: int) -> None:
        kind = type(event).__name__
        if kind == "AssistantMessage" and getattr(event, "text", ""):
            self.bus.emit("droid.message", round=rnd, text=event.text)
        elif kind == "ToolCall":
            self.bus.emit("droid.tool_call", round=rnd, name=event.name, input=_short(event.input))
        elif kind == "ToolResult":
            self.bus.emit("droid.tool_result", round=rnd, tool=event.tool_name, is_error=event.is_error,
                          content=_short(event.content))
        elif kind == "ErrorEvent":
            self.bus.emit("error", where="droid", message=getattr(event, "message", repr(event)))

    def _images(self, snapshot_path: Optional[str]):
        if not (self.d.get("attach_snapshot", True) and snapshot_path and Path(snapshot_path).exists()):
            return None
        from droid_sdk import Base64ImageSource, Image

        data = base64.b64encode(Path(snapshot_path).read_bytes()).decode()
        return [Image(Base64ImageSource(data=data, media_type="image/jpeg"))]

    def workspace(self, run_id: str) -> Path:
        return workspace_dir(self.cfg, run_id)


def _succeeded(result) -> bool:
    return getattr(result, "subtype", None) == "success"


def _short(value, limit: int = 1500) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + " …"
