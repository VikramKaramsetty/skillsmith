"""What Droid can touch, and the tools it gets. See docs/NORTH_STAR.md "How Droid gets feedback".

- `decide()` is the permission policy the SDK's permission handler applies to every action:
  edits only to skill.py in the workspace, tool calls only to our `skillsmith` server,
  everything else refused with a reason Droid can read.
- `RepairTools` are our functions, exposed to Droid as SDK MCP tools. They run in our process,
  on the sim/fake check env only. The real arm is never reachable from here. The evidence tools
  (get_frame, get_object_track, compare_with_success; repair/evidence_tools.py) only read the
  recorded attempt.
- `REPAIR_RESULT_SCHEMA` is the structured result Droid must return each round.
- `append_lesson()` grows skills/lessons.md, which every future prompt includes.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from skillsmith.events import EventBus, NullBus
from skillsmith.repair import evidence_tools
from skillsmith.repair.report import _scene_table, attempt_report
from skillsmith.skills.runtime import run_skill
from skillsmith.skills.validator import validate
from skillsmith.types import AttemptResult, Scene, TaskSpec

MCP_SERVER_NAME = "skillsmith"

# Droid's own label for its edit; feeds analysis.should_escalate (same class after a
# "parameter" change -> the next evidence says "change the approach").
CHANGE_TYPES = ("parameter", "strategy")

REPAIR_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "diagnosis": {"type": "string", "description": "What went wrong, citing numbers from the trace or scene."},
        "change": {"type": "string", "description": "What you changed in skill.py and why it fixes the diagnosis."},
        "change_type": {"type": "string", "enum": list(CHANGE_TYPES),
                        "description": "'parameter' if you only changed numbers or constants, "
                                       "'strategy' if you changed the approach (order, calls, logic)."},
        "sim_checked": {"type": "boolean", "description": "True if simulate_skill passed on your final skill.py."},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "lesson": {"type": "string", "description": "One reusable rule for future skills, or an empty string."},
        "skill_source": {"type": "string",
                         "description": "The COMPLETE new skill.py (whole file). The orchestrator saves it, "
                                        "re-checks it and runs it; this is how your fix is applied."},
    },
    "required": ["diagnosis", "change", "change_type", "sim_checked", "confidence", "lesson", "skill_source"],
    "additionalProperties": False,
}

_FILE_ACTIONS = ("EditAction", "CreateFile", "ApplyPatchAction")


# ---- permission policy -------------------------------------------------------------------
def _resolve(path: str, workspace: Path) -> Path:
    p = Path(path)
    return (p if p.is_absolute() else workspace / p).resolve()


def action_target(action) -> str:
    for attr in ("file_path", "full_command", "tool_name", "target", "command"):
        value = getattr(action, attr, None)
        if value:
            return str(value)
    return ""


def decide(action, workspace: Path) -> Tuple[bool, str]:
    """(allowed, reason) for one droid_sdk permission action."""
    kind = type(action).__name__
    if kind in _FILE_ACTIONS:
        allowed = _resolve("skill.py", workspace)
        paths = [getattr(action, "file_path", "")]
        paths += [getattr(f, "file_path", "") for f in (getattr(action, "files", None) or [])]
        for p in paths:
            if p and _resolve(p, workspace) != allowed:
                return False, f"only skill.py may be edited (refused {p})"
        return True, "edit to skill.py"
    if kind == "McpToolAction":
        server = getattr(action, "server_name", None)
        name = getattr(action, "actual_tool_name", None) or getattr(action, "tool_name", "")
        if server == MCP_SERVER_NAME or (server is None and any(name.endswith(t) for t in TOOL_NAMES)):
            return True, f"skillsmith tool {name}"
        return False, f"only skillsmith tools are allowed (refused {server}/{name})"
    if kind == "ExecuteAction":
        return False, "running commands is not allowed; use validate_skill and simulate_skill"
    return False, f"{kind} is not allowed in Skillsmith"


# ---- tools ---------------------------------------------------------------------------------
TOOL_NAMES = ("read_file", "write_skill", "validate_skill", "simulate_skill", "get_scene", "list_library",
              "read_library_skill", "get_frame", "get_object_track", "compare_with_success")

MAX_READ_CHARS = 20_000
EVIDENCE_TOOLS = ("get_frame", "get_object_track", "compare_with_success")  # evidence v2 only


class RepairTools:
    """State for one run. `update()` is called at the start of every round."""

    def __init__(self, cfg, bus: EventBus, workspace: Path, task: TaskSpec, library: Dict[str, str],
                 make_check_env: Callable[[Scene], object], sim_budget: int = 3, evidence_tools: bool = True):
        self.cfg = cfg
        self.bus = bus
        self.workspace = workspace
        self.task = task
        self.library = library
        self.make_check_env = make_check_env
        self.sim_budget = sim_budget
        self.evidence_tools = evidence_tools  # False in evidence v1 (the benchmark baseline)
        self.scene: Scene | None = None
        self.round = 0
        self.sim_runs = 0
        self.attempt: Optional[AttemptResult] = None   # the failed run this round repairs
        self.expected: Optional[AttemptResult] = None  # a passing run of the same skill (sim gate)
        self.last_pass: Optional[AttemptResult] = None  # Droid's latest passing simulate_skill run

    def update(self, scene: Scene | None, round_: int, library: Dict[str, str],
               attempt: Optional[AttemptResult] = None, expected: Optional[AttemptResult] = None) -> None:
        self.scene, self.round, self.library, self.sim_runs = scene, round_, library, 0
        self.attempt, self.expected = attempt, expected

    def _source(self) -> str:
        return (self.workspace / "skill.py").read_text()

    def validate_skill(self) -> str:
        issues = validate(self._source())
        self.bus.emit("droid.check", round=self.round, tool="validate_skill", ok=not issues)
        if not issues:
            return "PASS: skill.py passes all static checks."
        return "FAIL: fix every issue:\n" + "\n".join(f"- {i}" for i in issues)

    def simulate_skill(self) -> str:
        source = self._source()
        issues = validate(source)
        if issues:
            return "Not simulated: static checks fail. Call validate_skill and fix them first."
        if self.sim_runs >= self.sim_budget:
            return (f"Simulation budget for this round used up ({self.sim_budget} runs). "
                    "Finish with your best version; the orchestrator will simulate it again.")
        if self.scene is None:
            return "Not simulated: no scene available."
        self.sim_runs += 1
        env = self.make_check_env(self.scene)
        try:
            result = run_skill(source, env, self.task.params, self.cfg, NullBus(), skill=self.task.skill,
                               version=-1, library=self.library, success=self.task.success)
        finally:
            env.close()
        self.bus.emit("droid.check", round=self.round, tool="simulate_skill", run=self.sim_runs,
                      ok=result.ok, reason=result.reason())
        if result.ok:
            self.last_pass = result
        if result.ok:  # a pass is not a failure report: short and unambiguous
            detail = result.verify.detail if result.verify else "no success check"
            return (f"PASS: the task succeeded in simulation ({detail}; {len(result.trace)} calls, "
                    f"safety: {result.safety.summary()}).\n\nScene after:\n{_scene_table(result.scene_after)}")
        return f"FAIL: {result.reason()}\n\n{attempt_report(self.task, result, [])}"

    def read_file(self, name: str) -> str:
        """Read a text file inside the workspace (rules, evidence, skill.py, lessons, library/*.py)."""
        root = self.workspace.resolve()
        target = (root / name).resolve()
        if os.path.commonpath([root, target]) != str(root):
            self.bus.emit("droid.check", round=self.round, tool="read_file", ok=False, reason=f"outside workspace: {name}")
            return f"Refused: '{name}' is outside your workspace."
        if not target.is_file():
            files = sorted(str(f.relative_to(root)) for f in root.rglob("*")
                           if f.is_file() and f.suffix in (".md", ".py", ".json"))
            return f"No file '{name}'. Readable files: {', '.join(files)}"
        if target.suffix.lower() in (".jpg", ".jpeg", ".png"):
            return f"'{name}' is an image; it is attached to the prompt or described by get_frame."
        return target.read_text(errors="replace")[:MAX_READ_CHARS]

    def write_skill(self, source: str) -> str:
        """Replace skill.py with the full new source. The ONLY way Droid changes the skill."""
        (self.workspace / "skill.py").write_text(source)
        issues = validate(source)
        self.bus.emit("droid.check", round=self.round, tool="write_skill", ok=not issues,
                      lines=source.count("\n") + 1)
        if not issues:
            return "Saved skill.py. Static checks PASS. Next: simulate_skill."
        listed = "\n".join(f"- {i}" for i in issues)
        return f"Saved skill.py, but static checks FAIL (fix and call write_skill again):\n{listed}"

    def get_scene(self) -> str:
        return _scene_table(self.scene)

    def list_library(self) -> str:
        return "\n".join(f"- {name}" for name in sorted(self.library)) or "(empty)"

    def read_library_skill(self, name: str) -> str:
        return self.library.get(name, f"No saved skill named '{name}'. Call list_library.")

    def get_frame(self, phase: str = "") -> str:
        return evidence_tools.get_frame(self.attempt, phase, self.workspace, self.round)

    def get_object_track(self, name: str = "") -> str:
        return evidence_tools.get_object_track(self.attempt, name)

    def compare_with_success(self, phase: str = "") -> str:
        reference, kind = self.expected, "the sim gate run of this skill"
        if reference is None and self.last_pass is not None:
            reference, kind = self.last_pass, "your last passing simulate_skill run"
        return evidence_tools.compare_with_success(self.attempt, reference, kind, phase, self.workspace, self.round)

    def functions(self) -> List[Tuple[str, str, Callable]]:
        """(name, description, function) for each SDK MCP tool (no EVIDENCE_TOOLS in v1)."""
        return [t for t in self._all_functions() if self.evidence_tools or t[0] not in EVIDENCE_TOOLS]

    def _all_functions(self) -> List[Tuple[str, str, Callable]]:
        return [
            ("read_file", "Read a file in your workspace: AGENTS.md, API.md, LESSONS.md, failure.md, skill.py, "
                          "scene.json or library/<skill>.py.", self.read_file),
            ("write_skill", "Save the COMPLETE new skill.py (the whole file, not a diff). This is the only way "
                            "to change the skill; the static checker runs immediately.", self.write_skill),
            ("validate_skill", "Run the static safety checker on your current skill.py.", self.validate_skill),
            ("simulate_skill", "Run your current skill.py in simulation on a copy of the real table. "
                               "Returns PASS/FAIL, the trace and the safety report. Limited runs per round.",
             self.simulate_skill),
            ("get_scene", "Table of the objects currently detected on the table.", self.get_scene),
            ("list_library", "Names of saved, working skills you can call with skills.call().", self.list_library),
            ("read_library_skill", "Source code of a saved skill.", self.read_library_skill),
            ("get_frame", "One phase of the failed attempt (approach, grasp, lift, carry, place, release, "
                          "retreat, or crop_before/crop_after): its check, its calls and its annotated "
                          "key frame, copied into frames/ for you to Read. Empty phase = the failing "
                          "phase.", self.get_frame),
            ("get_object_track", "Where one object (e.g. 'blue cup') was through the failed attempt: "
                                 "camera positions before, at each look() and after, plus the stretch "
                                 "it rode in the gripper.", self.get_object_track),
            ("compare_with_success", "One phase of the failed attempt next to the same phase of a run "
                                     "that passed (the sim gate, else your last passing simulate_skill): "
                                     "checks, call by call tip positions, frames and end positions. "
                                     "Empty phase = the failing phase.",
             self.compare_with_success),
        ]


# ---- lessons -------------------------------------------------------------------------------
LESSONS_HEADER = ("# Lessons Droid has learned\n\n"
                  "One line per lesson, written by Droid after a successful repair. "
                  "Every Droid prompt includes this file.\n\n")


def append_lesson(path: Path, task_text: str, lesson: str) -> bool:
    lesson = " ".join(lesson.split())
    if not lesson:
        return False
    existing = path.read_text() if path.exists() else LESSONS_HEADER
    if lesson in existing:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing + f"- {time.strftime('%Y-%m-%d')} ({task_text}): {lesson}\n")
    return True
