"""Failure reports: the markdown Droid reads. Specific numbers beat prose."""

from __future__ import annotations

from typing import List, Optional

from skillsmith.types import AttemptResult, Scene, TaskSpec


def _fmt_xyz(p) -> str:
    return "—" if p is None else f"({p[0]:.3f}, {p[1]:.3f}, {p[2]:.3f})"


def _scene_table(scene: Optional[Scene]) -> str:
    if scene is None:
        return "_no scene_\n"
    rows = ["| label | x | y | z (base) | top | width |", "|---|---|---|---|---|---|"]
    rows += [f"| {o.label} | {o.x:.3f} | {o.y:.3f} | {o.z:.3f} | {o.top:.3f} | {o.width:.3f} |"
             for o in scene.objects]
    return "\n".join(rows) + "\n"


def _trace_table(attempt: AttemptResult, limit: int = 40) -> str:
    rows = ["| # | call | args | tip before | tip after | ok | result / error |",
            "|---|---|---|---|---|---|---|"]
    for c in attempt.trace[:limit]:
        args = ", ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
                         for k, v in c.args.items() if v is not None)
        res = c.error or ("" if c.result is None else str(c.result))
        rows.append(f"| {c.index} | {c.name} | {args} | {_fmt_xyz(c.ee_before)} | "
                    f"{_fmt_xyz(c.ee_after)} | {'✓' if c.ok else '✗'} | {res} |")
    if len(attempt.trace) > limit:
        rows.append(f"| … | {len(attempt.trace) - limit} more calls | | | | | |")
    return "\n".join(rows) + "\n"


def attempt_report(task: TaskSpec, attempt: AttemptResult, history: List[str]) -> str:
    stage = {"real": "the REAL arm", "sim": "simulation", "fake": "the test world"}.get(attempt.env, attempt.env)
    parts = [
        f"# Failure report: `{attempt.skill}` v{attempt.version} failed on {stage}\n",
        f"**Task:** {task.text}\n",
        f"**Params:** `{task.params}`\n",
        f"**Why it failed:** {attempt.reason()}\n",
    ]
    if attempt.verify:
        parts.append(f"**Camera check:** {'PASS' if attempt.verify.ok else 'FAIL'}: {attempt.verify.detail}\n")
    parts.append(f"**Safety:** {attempt.safety.summary()}\n")
    parts.append("## Scene before\n" + _scene_table(attempt.scene_before))
    parts.append("## Scene after\n" + _scene_table(attempt.scene_after))
    parts.append("## Trace\n" + _trace_table(attempt))
    parts.append(_history(history))
    return "\n".join(parts)


def static_report(task: TaskSpec, issues) -> str:
    lines = "\n".join(f"- {i}" for i in issues)
    return (f"# Static check failed\n\n**Task:** {task.text}\n\n"
            f"The skill was rejected before running. Fix every issue:\n\n{lines}\n")


def rejection_report(task: TaskSpec, note: str, history: List[str]) -> str:
    return (f"# Human reviewer rejected the skill\n\n**Task:** {task.text}\n\n"
            f"**Reviewer note:** {note or '(no note)'}\n\n{_history(history)}")


def draft_brief(task: TaskSpec, scene: Optional[Scene]) -> str:
    return (f"# New task: write a skill\n\n**Task:** {task.text}\n\n"
            f"**Skill name:** `{task.skill}`  **Params:** `{task.params}`\n\n"
            f"**Success check (run by the camera afterwards):** `{task.success}`\n\n"
            f"## Scene now\n{_scene_table(scene)}")


def _history(history: List[str]) -> str:
    if not history:
        return ""
    return "## Earlier rounds (do not repeat these)\n" + "\n".join(f"- {h}" for h in history) + "\n"
