"""DroidAuthor (CLI FALLBACK): runs Factory `droid exec` headless to write or repair one skill.

The product path is DroidSDKAuthor in droid_sdk.py (docs/NORTH_STAR.md). Use this only if the
SDK breaks on stage (`droid.mode: cli`), and say so.

Permissions are controlled here, not trusted to Droid:
- `--auto low`: file edits only (no installs, no system changes, no remote writes).
- `--remove-tools Execute` (config): Droid cannot run commands; WE run static checks,
  sim and safety after it returns.
- The workspace lives OUTSIDE the repo (~/.skillsmith/droid_ws/<run_id>), so Droid sees
  only droid_template/AGENTS.md, API.md, skill.py, failure.md, scene.json and read-only
  library copies. We read back skill.py and ignore everything else.

WS5 TODO: confirm the stream-json event field names against a real `droid exec` run and
tighten `_session_id` / `_final_text`. Docs: https://docs.factory.ai/cli/droid-exec/overview
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import List, Optional

from skillsmith.events import EventBus
from skillsmith.repair.protocols import AuthorContext
from skillsmith.repair.report import draft_brief
from skillsmith.repair.workspace import prepare_workspace, workspace_dir
from skillsmith.types import TaskSpec

PROMPT_HEADER = """\
You are repairing a robot skill. First read AGENTS.md and API.md in this folder.
Edit ONLY skill.py. Do not create other files. Do not try to run anything: the
orchestrator runs static checks, a MuJoCo simulation and safety checks after you finish.
When done, reply with 2-3 sentences: what went wrong and what you changed.

---

"""

SKILL_STUB = '''"""{name}: TODO one-line description."""

SKILL = {{"name": "{name}", "params": {params}, "uses": []}}


def run(robot, scene, skills, {args}):
    raise SkillError("not written yet")
'''


class DroidAuthor:
    def __init__(self, cfg, bus: EventBus):
        self.cfg = cfg
        self.bus = bus
        self.d = cfg.droid
        self.session_ids: dict = {}

    # ---- SkillAuthor ----------------------------------------------------------------
    def draft(self, task: TaskSpec, ctx: AuthorContext) -> str:
        params = {k: type(v).__name__ for k, v in task.params.items()}
        stub = SKILL_STUB.format(name=task.skill, params=params, args=", ".join(task.params))
        return self._round(stub, draft_brief(task, ctx.scene), ctx)

    def repair(self, task: TaskSpec, source: str, report_md: str, ctx: AuthorContext) -> str:
        return self._round(source, report_md, ctx)

    # ---- internals ------------------------------------------------------------------
    def workspace(self, run_id: str) -> Path:
        return workspace_dir(self.cfg, run_id)

    def _round(self, source: str, report_md: str, ctx: AuthorContext) -> str:
        ws = prepare_workspace(self.cfg, source, PROMPT_HEADER + report_md, report_md, ctx)
        code = self._exec(ws, ctx)
        new = (ws / "skill.py").read_text()
        if code != 0 and new == source:
            raise RuntimeError(f"droid exec exited {code} without changing skill.py")
        return new

    def command(self, ws: Path, run_id: str) -> List[str]:
        cmd = [self.d.bin, "exec", "-f", str(ws / "failure.md"), "--auto", self.d.auto,
               "-o", "stream-json", "--cwd", str(ws)]
        if self.d.model:
            cmd += ["-m", self.d.model]
        if self.d.reasoning:
            cmd += ["-r", self.d.reasoning]
        if self.d.remove_tools:
            cmd += ["--remove-tools", ",".join(self.d.remove_tools)]
        if run_id in self.session_ids:
            cmd += ["--session-id", self.session_ids[run_id]]
        return cmd

    def _exec(self, ws: Path, ctx: AuthorContext) -> int:
        cmd = self.command(ws, ctx.run_id)
        self.bus.emit("droid.started", round=ctx.round, cmd=" ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        timer = threading.Timer(self.d.timeout_s, proc.kill)
        timer.start()
        final_text = ""
        try:
            for line in proc.stdout:
                event = _parse(line)
                if event is None:
                    continue
                self.bus.emit("droid.event", round=ctx.round, event=event)
                sid = _session_id(event)
                if sid:
                    self.session_ids[ctx.run_id] = sid
                final_text = _final_text(event) or final_text
            proc.wait()
        finally:
            timer.cancel()
        stderr = proc.stderr.read() if proc.stderr else ""
        if proc.returncode != 0:
            self.bus.emit("error", where="droid", message=stderr[-2000:] or f"exit {proc.returncode}")
        self.bus.emit("droid.finished", round=ctx.round, exit_code=proc.returncode,
                      session_id=self.session_ids.get(ctx.run_id), summary=final_text)
        return proc.returncode


def _parse(line: str) -> Optional[dict]:
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return {"type": "raw", "text": line}
    return event if isinstance(event, dict) else {"type": "raw", "value": event}


def _session_id(event: dict) -> Optional[str]:
    for key in ("session_id", "sessionId"):
        if isinstance(event.get(key), str):
            return event[key]
    for value in event.values():
        if isinstance(value, dict):
            found = _session_id(value)
            if found:
                return found
    return None


def _final_text(event: dict) -> Optional[str]:
    if event.get("type") in ("completion", "result"):
        for key in ("result", "text", "finalText", "message"):
            if isinstance(event.get(key), str):
                return event[key]
    return None
