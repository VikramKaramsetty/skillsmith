"""Failure reports: the markdown Droid reads. Specific numbers beat prose.

Evidence v2 (docs/NORTH_STAR.md, docs/ARCHITECTURE.md "Droid integration") leads with the
failure class, then:

    classification line → phase table → task/params/success check → camera check → safety
    → annotated key frames + cropped before/after pair → expected vs actual (object positions
    the success check wanted, plus the passing sim check's phases/frames when one ran)
    → trace with the failing call marked → scene tables → earlier rounds → lessons

Images are markdown links (`![caption](path)`); `evidence_images()` pulls them back out so the
author can attach them to Droid's call and copy them into the workspace.
"""

from __future__ import annotations

import dataclasses
import math
import re
from typing import List, Optional, Tuple

from skillsmith.types import PHASES, AttemptResult, Scene, TaskSpec

CROP_KEYS = ("crop_before", "crop_after")  # keyframes keys for WS3.8's cropped before/after pair
MAX_LESSONS = 12
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def _fmt_xyz(p) -> str:
    return "—" if p is None else f"({p[0]:.3f}, {p[1]:.3f}, {p[2]:.3f})"


def _scene_table(scene: Optional[Scene]) -> str:
    if scene is None:
        return "_no scene_\n"
    rows = ["| label | x | y | z (base) | top | width |", "|---|---|---|---|---|---|"]
    rows += [f"| {o.label} | {o.x:.3f} | {o.y:.3f} | {o.z:.3f} | {o.top:.3f} | {o.width:.3f} |"
             for o in scene.objects]
    return "\n".join(rows) + "\n"


def _failing_index(attempt: AttemptResult) -> Optional[int]:
    """The call to point Droid at: the classifier's, else the first failed call, else the last call."""
    c = attempt.classification
    if c is not None and c.call_index is not None:
        return c.call_index
    failed = [call.index for call in attempt.trace if not call.ok]
    if failed:
        return failed[0]
    if attempt.error and attempt.trace:
        return attempt.trace[-1].index
    return None


def _trace_table(attempt: AttemptResult, limit: int = 40, calls: Optional[list] = None) -> str:
    """The attempt's trace (or just `calls` from it), with the failing call marked."""
    calls = attempt.trace if calls is None else calls
    rows = ["| # | call | args | tip before | tip after | ok | result / error |",
            "|---|---|---|---|---|---|---|"]
    failing = None if attempt.ok else _failing_index(attempt)
    for c in calls[:limit]:
        args = ", ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
                         for k, v in c.args.items() if v is not None)
        res = c.error or ("" if c.result is None else str(c.result))
        if c.index == failing:
            res = f"**← failed here** {res}".rstrip()
        rows.append(f"| {c.index} | {c.name} | {args} | {_fmt_xyz(c.ee_before)} | "
                    f"{_fmt_xyz(c.ee_after)} | {'✓' if c.ok else '✗'} | {res} |")
    if len(calls) > limit:
        rows.append(f"| … | {len(calls) - limit} more calls | | | | | |")
    return "\n".join(rows) + "\n"


# ---- evidence v2 sections -------------------------------------------------------------------
def _classification(attempt: AttemptResult) -> List[str]:
    c = attempt.classification
    if c is None:
        return ["**Failure class:** `UNKNOWN` (unclassified: failure analysis gave no label; "
                "diagnose from all the evidence below)\n"]
    at = f"call #{c.call_index}" if c.call_index is not None else "call ?"
    flag = " **(low confidence: check the evidence before trusting the label)**" if c.bucket == "unknown" else ""
    out = [f"**Failure class:** `{c.cls}` ({c.bucket}, confidence {c.confidence:.2f}) in phase "
           f"**{c.phase or '?'}** at {at}{flag}\n",
           f"**What happened:** {c.summary}\n"]
    if c.hint:
        out.append(f"**Look at:** {c.hint}\n")
    if c.escalate:
        out.append("**A value-only change already failed for this class. Change the approach, "
                   "not just a number.**\n")
    return out


def _phase_table(attempt: AttemptResult) -> str:
    if not attempt.phases:
        return "## Phases\n_no phase checks for this attempt_\n"
    failed = attempt.classification.phase if attempt.classification else None
    rows = ["| phase | ok | calls | detail | frame |", "|---|---|---|---|---|"]
    for p in attempt.phases:
        name = f"**{p.phase}** ←" if p.phase == failed else p.phase
        frame = p.frame_path or attempt.keyframes.get(p.phase, "")
        rows.append(f"| {name} | {'✓' if p.ok else '✗'} | #{p.start_index}–#{p.end_index} | {p.detail} | {frame} |")
    return "## Phases\n" + "\n".join(rows) + "\n"


def _pred_text(pred: dict) -> str:
    op, args = pred.get("op", "?"), pred.get("args", [])
    if op in ("all", "any", "not"):
        return f"{op}(" + ", ".join(_pred_text(a) for a in args) + ")"
    return f"{op}(" + ", ".join(str(a) for a in args) + ")"


def _phase_order(phase: str) -> int:
    return PHASES.index(phase) if phase in PHASES else len(PHASES)


def _ordered_frames(attempt: AttemptResult) -> List[Tuple[str, str]]:
    """(phase, path) for the annotated key frames, failing phase first, crops excluded."""
    frames = {k: v for k, v in attempt.keyframes.items() if k not in CROP_KEYS and v}
    for p in attempt.phases:
        if p.frame_path and p.phase not in frames:
            frames[p.phase] = p.frame_path
    failed = attempt.classification.phase if attempt.classification else None
    return sorted(frames.items(), key=lambda kv: (kv[0] != failed, _phase_order(kv[0])))


def _key_frames(attempt: AttemptResult) -> str:
    frames = _ordered_frames(attempt)
    crops = [(k, attempt.keyframes[k]) for k in CROP_KEYS if attempt.keyframes.get(k)]
    if not frames and not crops:
        return ""
    details = {p.phase: p.detail for p in attempt.phases}
    lines = ["## Key frames",
             "Annotated: boxes + labels, target crosshair, gripper tip, error arrow in cm."]
    for phase, path in frames:
        caption = f"{phase}: {details[phase]}" if details.get(phase) else phase
        lines.append(f"![{caption}]({path})")
    if crops:
        lines.append("Cropped before / after around the objects in the success check:")
        lines += [f"![{k.replace('crop_', '')}]({path})" for k, path in crops]
    return "\n".join(lines) + "\n"


def _expected_positions(pred: dict, scene: Scene) -> List[Tuple[str, Tuple, str]]:
    """(object, (x, y, z | None), why) the success check wants, relative to objects in `scene`."""
    op, args = pred.get("op"), pred.get("args", [])
    if op in ("all", "any"):
        return [row for a in args for row in _expected_positions(a, scene)]
    if op in ("on_top", "inside") and len(args) == 2:
        ref = scene.find(args[1])
        if ref is None:
            return []
        if op == "on_top":
            return [(str(args[0]), (ref.x, ref.y, ref.top), f"on top of {ref.label}")]
        return [(str(args[0]), (ref.x, ref.y, None), f"inside {ref.label}")]
    if op == "near" and len(args) == 3:
        return [(str(args[0]), (float(args[1]), float(args[2]), None), "near the given point")]
    return []


def _position_rows(task: TaskSpec, attempt: AttemptResult) -> List[str]:
    scene = attempt.scene_after or attempt.scene_before
    if scene is None:
        return []
    rows = []
    for label, (x, y, z), why in _expected_positions(task.success, scene):
        obj = scene.find(label)
        exp = f"({x:.3f}, {y:.3f}, {'—' if z is None else f'{z:.3f}'})"
        if obj is None:
            rows.append(f"| {label} | {why} | {exp} | not seen | — | — |")
            continue
        dxy = math.hypot(obj.x - x, obj.y - y) * 100
        dz = "—" if z is None else f"{(obj.z - z) * 100:+.1f}"
        rows.append(f"| {label} | {why} | {exp} | ({obj.x:.3f}, {obj.y:.3f}, {obj.z:.3f}) | {dxy:.1f} | {dz} |")
    return rows


def _expected_vs_actual(task: TaskSpec, attempt: AttemptResult, expected: Optional[AttemptResult]) -> str:
    parts = []
    rows = _position_rows(task, attempt)
    if rows:
        parts.append("### Object positions: what the success check wanted vs where they ended up\n"
                     "| object | wanted | expected (x, y, z) | actual (x, y, z) | xy error cm | z error cm |\n"
                     "|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n")
    if expected is not None:
        parts.append(_compare_runs(attempt, expected))
    if not parts:
        return ""
    return "## Expected vs actual\n" + "\n".join(parts)


def _compare_runs(attempt: AttemptResult, expected: AttemptResult) -> str:
    """Phase by phase: the run that passed the gates (sim/fake check) vs this attempt."""
    out = [f"### This skill passed on {expected.env} (v{expected.version}) but failed on {attempt.env}"]
    exp = {p.phase: p for p in expected.phases}
    act = {p.phase: p for p in attempt.phases}
    names = list(exp) + [n for n in act if n not in exp]
    if names:
        out += [f"| phase | expected ({expected.env}) | actual ({attempt.env}) |", "|---|---|---|"]
        for n in names:
            e, a = exp.get(n), act.get(n)
            fe = f"{'✓' if e.ok else '✗'} {e.detail}" if e else "—"
            fa = f"{'✓' if a.ok else '✗'} {a.detail}" if a else "— (not reached)"
            out.append(f"| {n} | {fe} | {fa} |")
    for n, e_path in expected.keyframes.items():
        if n in CROP_KEYS or not e_path:
            continue
        out.append(f"![expected {n} ({expected.env})]({e_path})")
        a_path = attempt.keyframes.get(n)
        out.append(f"![actual {n} ({attempt.env})]({a_path})" if a_path else f"_no {attempt.env} frame for {n}_")
    if expected.scene_after is not None and attempt.scene_after is not None:
        out.append("\n" + _scene_delta(expected.scene_after, attempt.scene_after, expected.env, attempt.env))
    return "\n".join(out) + "\n"


def _scene_delta(exp: Scene, act: Scene, exp_env: str, act_env: str) -> str:
    rows = [f"| object | end ({exp_env}) | end ({act_env}) | apart cm |", "|---|---|---|---|"]
    for o in exp.objects:
        a = act.find(o.label)
        if a is None:
            rows.append(f"| {o.label} | ({o.x:.3f}, {o.y:.3f}, {o.z:.3f}) | not seen | — |")
            continue
        d = math.dist((o.x, o.y, o.z), (a.x, a.y, a.z)) * 100
        if d >= 0.5:
            rows.append(f"| {o.label} | ({o.x:.3f}, {o.y:.3f}, {o.z:.3f}) | ({a.x:.3f}, {a.y:.3f}, {a.z:.3f}) | {d:.1f} |")
    return "\n".join(rows) if len(rows) > 2 else "End positions match within 0.5 cm."


def _lessons(lessons: Optional[str]) -> str:
    items = [ln for ln in (lessons or "").splitlines() if ln.startswith("- ")]
    if not items:
        return ""
    shown = items[-MAX_LESSONS:]
    more = f" (latest {len(shown)} of {len(items)}; all in LESSONS.md)" if len(items) > len(shown) else ""
    return f"## Lessons from earlier tasks{more}\n" + "\n".join(shown) + "\n"


def attempt_report(task: TaskSpec, attempt: AttemptResult, history: List[str], *,
                   expected: Optional[AttemptResult] = None, lessons: Optional[str] = None) -> str:
    """Evidence v2 for one failed attempt. `expected` is a run of the same skill that passed
    (the sim gate); `lessons` is the text of skills/lessons.md."""
    stage = {"real": "the REAL arm", "sim": "simulation", "fake": "the test world"}.get(attempt.env, attempt.env)
    parts = [f"# Failure report: `{attempt.skill}` v{attempt.version} failed on {stage}\n"]
    parts += _classification(attempt)
    parts.append(_phase_table(attempt))
    parts += [
        "## Task",
        f"**Task:** {task.text}\n",
        f"**Params:** `{task.params}`\n",
        f"**Success check:** `{_pred_text(task.success)}`\n",
        f"**Why it failed:** {attempt.reason()}\n",
    ]
    if attempt.verify:
        parts.append(f"**Camera check:** {'PASS' if attempt.verify.ok else 'FAIL'}: {attempt.verify.detail}\n")
    parts.append(f"**Safety:** {attempt.safety.summary()}\n")
    parts += [s for s in (_key_frames(attempt), _expected_vs_actual(task, attempt, expected)) if s]
    parts.append("## Trace\n" + _trace_table(attempt))
    parts.append("## Scene before\n" + _scene_table(attempt.scene_before))
    parts.append("## Scene after\n" + _scene_table(attempt.scene_after))
    parts += [s for s in (_history(history), _lessons(lessons)) if s]
    return "\n".join(parts)


def unlabelled_reason(attempt: AttemptResult) -> str:
    """attempt.reason() without the failure class: the raw error / safety / camera line."""
    return dataclasses.replace(attempt, classification=None).reason()


def attempt_report_v1(task: TaskSpec, attempt: AttemptResult, history: List[str]) -> str:
    """The pre-v2 report, for the WS5.6 benchmark baseline: why it failed, trace and scenes.
    No failure class, phase table, key frames, expected-vs-actual or lessons."""
    stage = {"real": "the REAL arm", "sim": "simulation", "fake": "the test world"}.get(attempt.env, attempt.env)
    parts = [f"# Failure report: `{attempt.skill}` v{attempt.version} failed on {stage}\n",
             f"**Task:** {task.text}\n",
             f"**Params:** `{task.params}`\n",
             f"**Success check:** `{_pred_text(task.success)}`\n",
             f"**Why it failed:** {unlabelled_reason(attempt)}\n"]
    if attempt.verify:
        parts.append(f"**Camera check:** {'PASS' if attempt.verify.ok else 'FAIL'}: {attempt.verify.detail}\n")
    parts.append(f"**Safety:** {attempt.safety.summary()}\n")
    parts.append("## Trace\n" + _trace_table(attempt))
    parts.append("## Scene before\n" + _scene_table(attempt.scene_before))
    parts.append("## Scene after\n" + _scene_table(attempt.scene_after))
    if history:
        parts.append(_history(history))
    return "\n".join(parts)


def evidence_images(report_md: str) -> List[str]:
    """Image paths linked from a report, in order, without duplicates."""
    return list(dict.fromkeys(_IMAGE_RE.findall(report_md or "")))


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
