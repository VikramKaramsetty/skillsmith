"""The self-repair state machine. See docs/ARCHITECTURE.md "The self-repair loop".

    PLAN → (gates if new code) → ATTEMPT on target → success? SAVE
                                        │ fail
                                        ▼
            REPAIR (author) → STATIC → SIM → SAFETY → APPROVE → ATTEMPT ...

Every code change made by the author passes all gates before the target env runs it.
Library skills (already approved) go straight to the target.

Environment-bucket failures (camera moved, object out of reach, ...) never reach the author:
the loop asks a human to fix the table, then retries the same code. Before each repair the
classification is marked `escalate` when the same class repeats after a "parameter" change.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from skillsmith.events import EventBus
from skillsmith.hal.base import Env
from skillsmith.repair.protocols import Approver, RepairContext, SkillAuthor
from skillsmith.repair.report import attempt_report, rejection_report, static_report
from skillsmith.repair.workspace import lessons_path
from skillsmith.skills.library import SkillLibrary
from skillsmith.skills.runtime import run_skill
from skillsmith.skills.validator import validate
from skillsmith.types import BUCKET_ENVIRONMENT, AttemptResult, Classification, Scene, TaskSpec


@dataclass
class RunResult:
    ok: bool
    rounds: int
    source: str
    version: Optional[int]
    attempts: List[AttemptResult] = field(default_factory=list)


class RepairLoop:
    def __init__(self, cfg, target: Env, make_check_env: Optional[Callable[[Scene], Env]],
                 library: SkillLibrary, author: SkillAuthor, approver: Approver, bus: EventBus):
        if target.name == "real" and make_check_env is None and cfg.safety.require_sim_before_real:
            raise ValueError("the real arm requires a sim check env (safety.require_sim_before_real)")
        self.cfg = cfg
        self.target = target
        self.make_check_env = make_check_env
        self.library = library
        self.author = author
        self.approver = approver
        self.bus = bus
        self.history: List[str] = []
        self._last_check: Optional[AttemptResult] = None
        self._gate_failure: Optional[AttemptResult] = None  # the sim run the last gate rejected
        self._last_cls: Optional[Classification] = None     # the last failure class sent to the author

    # ---- main entry -----------------------------------------------------------------
    def run(self, task: TaskSpec) -> RunResult:
        bus, max_rounds = self.bus, self.cfg.repair.max_rounds
        bus.emit("run.started", task=task)
        scene = self._observe()

        if self.library.has(task.skill):
            source, version, needs_gates = self.library.load(task.skill), self.library.version(task.skill), False
            bus.emit("plan.selected", skill=task.skill, source="library", version=version)
        else:
            source = self.author.draft(task, self._ctx(0, scene))
            version, needs_gates = 0, True
            bus.emit("plan.selected", skill=task.skill, source="draft", version=version)
            bus.emit("skill.diff", old_version=None, new_version=0, diff=_diff("", source))

        rounds, env_stops, attempts = 0, 0, []
        while True:
            if needs_gates:
                ok, report = self._gates(task, source, scene, rounds)
                if not ok:
                    if _needs_human(self._gate_failure):
                        if env_stops >= max_rounds or not self._ask_human(self._gate_failure):
                            self._environment_stop(self._gate_failure)
                            break
                        env_stops += 1
                        scene = self._observe()
                        continue
                    if rounds >= max_rounds:
                        break
                    rounds += 1
                    source = self._repair(task, source, report, rounds, scene, self._gate_failure)
                    continue
                approved, note = self._approve(task, source, rounds)
                if not approved:
                    if rounds >= max_rounds:
                        break
                    rounds += 1
                    report = rejection_report(task, note, self.history)
                    source = self._repair(task, source, report, rounds, scene)
                    continue

            attempt = self._attempt(task, source, rounds)
            attempts.append(attempt)
            while _needs_human(attempt) and env_stops < max_rounds and self._ask_human(attempt):
                env_stops += 1
                scene = self._observe()
                attempt = self._attempt(task, source, rounds)
                attempts.append(attempt)
            if attempt.ok:
                saved = self.library.save(task.skill, source) if needs_gates else None
                if saved is None:
                    self.library.record_success(task.skill)
                bus.emit("skill.saved", skill=task.skill, version=saved or version)
                bus.emit("run.finished", ok=True, rounds=rounds)
                return RunResult(True, rounds, source, saved or version, attempts)

            if _needs_human(attempt):
                self._environment_stop(attempt)  # never sent to the author as a code problem
                break
            if rounds >= max_rounds:
                break
            rounds += 1
            self._mark_escalation(attempt)
            expected = self._last_check if needs_gates else None  # the sim run of this same source
            report = attempt_report(task, attempt, self.history, expected=expected, lessons=self._lessons())
            self.history.append(f"v{attempt.version} on {attempt.env}: {attempt.reason()}")
            self._reset_scene()
            scene = self._observe()
            source = self._repair(task, source, report, rounds, scene, attempt, expected)
            needs_gates = True

        bus.emit("run.finished", ok=False, rounds=rounds)
        return RunResult(False, rounds, source, None, attempts)

    # ---- steps ----------------------------------------------------------------------
    def _gates(self, task: TaskSpec, source: str, scene: Scene, rnd: int) -> Tuple[bool, str]:
        self._gate_failure = None
        issues = validate(source)
        self.bus.emit("gate.result", gate="static", ok=not issues, detail="; ".join(map(str, issues)) or "ok")
        if issues:
            self.history.append(f"v{rnd} rejected by static checks: {issues[0]}")
            return False, static_report(task, issues)
        if self.make_check_env is None:
            self.bus.emit("gate.result", gate="sim", ok=True, detail="skipped (no check env)")
            return True, ""
        env = self.make_check_env(scene)
        try:
            sim = run_skill(source, env, task.params, self.cfg, self.bus, skill=task.skill,
                            version=rnd, library=self.library.sources(), success=task.success)
        finally:
            env.close()
        if sim.video_path:
            self.bus.emit("sim.video", path=sim.video_path)
        sim_ok = sim.error is None and (sim.verify.ok if sim.verify else True)
        self.bus.emit("gate.result", gate="sim", ok=sim_ok, detail=sim.reason() if not sim_ok else "task achieved in sim")
        self.bus.emit("gate.result", gate="safety", ok=sim.safety.ok, detail=sim.safety.summary())
        if sim_ok and sim.safety.ok:
            self._last_check = sim
            return True, ""
        self.history.append(f"v{rnd} failed in {env.name}: {sim.reason()}")
        self._gate_failure = sim
        if _needs_human(sim):
            return False, ""
        self._mark_escalation(sim)
        return False, attempt_report(task, sim, self.history, lessons=self._lessons())

    def _approve(self, task: TaskSpec, source: str, rnd: int) -> Tuple[bool, str]:
        check = self._last_check
        summary = {
            "title": f"run {task.skill} v{rnd} on {self.target.name}",
            "skill": task.skill, "version": rnd, "source_code": source,
            "sim": check.reason() if check else "skipped",
            "safety": check.safety.summary() if check else "n/a",
            "video": check.video_path if check else None,
        }
        self.bus.emit("approval.requested", summary=summary)
        approved, note = self.approver.request(self.bus.run_id, summary)
        self.bus.emit("approval.decided", approved=approved, note=note)
        if not approved:
            self.history.append(f"v{rnd} rejected by human: {note or 'no note'}")
        return approved, note

    def _attempt(self, task: TaskSpec, source: str, rnd: int) -> AttemptResult:
        return run_skill(source, self.target, task.params, self.cfg, self.bus, skill=task.skill,
                         version=rnd, library=self.library.sources(), success=task.success)

    def _ask_human(self, attempt: AttemptResult) -> bool:
        """An environment failure: a human fixes the table or camera, then approves a retry of
        the same code. A reject (or silence) ends the run. The author is never called."""
        c = attempt.classification
        self.history.append(f"v{attempt.version} on {attempt.env}: {attempt.reason()} (environment: sent to a human)")
        summary = {
            "kind": "environment",
            "title": f"{c.cls}: not a code problem. Fix the table or camera, then approve to retry",
            "class": c.cls, "phase": c.phase, "summary": c.summary, "hint": c.hint,
            "skill": attempt.skill, "version": attempt.version, "env": attempt.env,
        }
        self.bus.emit("approval.requested", summary=summary)
        fixed, note = self.approver.request(self.bus.run_id, summary)
        self.bus.emit("approval.decided", approved=fixed, note=note)
        if fixed:
            reset = getattr(self.target.arm, "reset_scene", None)
            if callable(reset):  # fake/sim: the world puts itself back; on the real arm the human did
                reset()
        return fixed

    def _environment_stop(self, attempt: AttemptResult) -> None:
        self.bus.emit("error", where="repair.environment",
                      message=f"stopped on {attempt.reason()}: a human must fix the environment; Droid was not called")

    def _mark_escalation(self, attempt: AttemptResult) -> None:
        """Same class again after a value-only change: tell the author to change the approach."""
        c = attempt.classification
        if c is None:
            return
        result = getattr(self.author, "last_result", None) or {}  # DroidSDKAuthor's structured result
        c.escalate = c.escalate or _escalate(self._last_cls, c, result.get("change_type"))
        self._last_cls = c

    def _repair(self, task: TaskSpec, source: str, report: str, rnd: int, scene: Scene,
                attempt: Optional[AttemptResult] = None, expected: Optional[AttemptResult] = None) -> str:
        """`attempt` (the failed run) and `expected` (a passing run of the same skill) back
        Droid's evidence tools; both are None after a static or reviewer rejection."""
        self.bus.emit("repair.started", round=rnd, reason=report.splitlines()[0] if report else "")
        snapshot = self._snapshot(rnd)
        ctx = self._ctx(rnd, scene, snapshot)
        ctx.attempt, ctx.expected = attempt, expected
        new = self.author.repair(task, source, report, ctx)
        self.bus.emit("skill.diff", old_version=rnd - 1, new_version=rnd, diff=_diff(source, new))
        return new

    # ---- helpers --------------------------------------------------------------------
    def _ctx(self, rnd: int, scene: Optional[Scene], snapshot: Optional[str] = None) -> RepairContext:
        return RepairContext(self.bus.run_id, rnd, scene, snapshot, self.library.sources())

    def _lessons(self) -> Optional[str]:
        path = lessons_path(self.cfg)
        return path.read_text() if path.exists() else None

    def _observe(self) -> Scene:
        scene = self.target.perception.observe()
        self.bus.emit("scene.observed", scene=scene, image_path=scene.image_path)
        return scene

    def _snapshot(self, rnd: int) -> Optional[str]:
        if self.bus.run_dir is None:
            return None
        path = Path(self.bus.run_dir) / f"snapshot_round{rnd}.jpg"
        return self.target.perception.snapshot(str(path))

    def _reset_scene(self) -> None:
        """Fake/sim worlds reset themselves; on the real arm a human resets the table."""
        reset = getattr(self.target.arm, "reset_scene", None)
        if callable(reset):
            reset()
            return
        self.bus.emit("reset.requested", message="Reset the objects to their start positions")
        self.approver.request(self.bus.run_id, {"title": "Reset the table, then approve", "kind": "reset"})


def _needs_human(attempt: Optional[AttemptResult]) -> bool:
    c = attempt.classification if attempt is not None else None
    return c is not None and c.bucket == BUCKET_ENVIRONMENT


def _escalate(previous: Optional[Classification], current: Classification, change_type: Optional[str]) -> bool:
    from skillsmith.analysis import should_escalate

    try:
        return should_escalate(previous, current, change_type)
    except NotImplementedError:  # WS4.6 not merged yet: the rule from analysis' contract
        return previous is not None and previous.cls == current.cls and change_type == "parameter"


def _diff(old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        "skill.py (before)", "skill.py (after)"))
