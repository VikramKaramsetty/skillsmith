"""Droid's evidence tools (WS5.4): pull more evidence on demand, like a human scrubbing video.

- `get_frame(phase)`: the phase check, its calls and its annotated key frame, copied into the
  workspace so Droid can open it with Read.
- `get_object_track(name)`: where one object was through the attempt: observed positions
  (scene before, every `look()`, scene after) plus the stretch it rode in the gripper.
- `compare_with_success(phase)`: one phase of the failed attempt next to the same phase of a
  run that passed (the sim gate, else Droid's own passing `simulate_skill`), call by call.

Pure functions over `AttemptResult`; `RepairTools` (droid_tools.py) exposes them as SDK tools.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path
from typing import List, Optional

from skillsmith.repair.report import CROP_KEYS, _fmt_xyz, _ordered_frames, _scene_delta, _trace_table
from skillsmith.types import AttemptResult, PhaseCheck, PrimitiveCall, Scene

NO_ATTEMPT = ("No failed attempt this round (the skill was rejected by the static checks or by a "
              "reviewer before it ran). Use the evidence in failure.md.")
NO_REFERENCE = ("No successful run to compare with yet. Call simulate_skill: once it passes, that run "
                "becomes the reference.")
HOLD_RADIUS = 0.04  # m: an object this close (xy) to the tip at a successful grasp is the held one


# ---- shared helpers ---------------------------------------------------------------------------
def _phase(attempt: AttemptResult, phase: str) -> str:
    """The phase asked for; empty means the phase the classifier blamed (or '' if none)."""
    phase = (phase or "").strip().lower()
    if not phase and attempt.classification is not None:
        return attempt.classification.phase or ""
    return phase


def _check(attempt: AttemptResult, phase: str) -> Optional[PhaseCheck]:
    return next((p for p in attempt.phases if p.phase == phase), None)


def _frame(attempt: AttemptResult, phase: str) -> Optional[str]:
    check = _check(attempt, phase)
    return (check.frame_path if check else None) or attempt.keyframes.get(phase) or None


def _calls(attempt: AttemptResult, check: Optional[PhaseCheck]) -> List[PrimitiveCall]:
    if check is None:
        return []
    return [c for c in attempt.trace if check.start_index <= c.index <= check.end_index]


def _available(attempt: AttemptResult) -> str:
    phases = [p.phase for p in attempt.phases]
    frames = [k for k, _ in _ordered_frames(attempt) if k not in phases]
    frames += [k for k in CROP_KEYS if attempt.keyframes.get(k)]
    names = phases + frames
    return ", ".join(names) if names else "none (no phase checks or key frames for this attempt)"


def _localise(path: str, ws: Path, tag: str) -> str:
    """Copy a frame into the workspace's frames/ so Droid can Read it; say so if it is missing."""
    src = Path(path)
    if not src.is_file():
        return f"`{path}` _(missing: the file was not saved)_"
    dest = ws / "frames" / f"{tag}_{src.name}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest)
    return f"`frames/{dest.name}` (open it with Read)"


def _check_line(check: Optional[PhaseCheck]) -> str:
    if check is None:
        return "— (not reached)"
    return f"{'✓' if check.ok else '✗'} {check.detail} (calls #{check.start_index}–#{check.end_index})"


def _call_text(call: Optional[PrimitiveCall]) -> str:
    if call is None:
        return "—"
    args = ", ".join(f"{v:.3f}" if isinstance(v, float) else str(v) for v in call.args.values() if v is not None)
    return f"#{call.index} {call.name}({args}){'' if call.ok else ' ✗'}"


# ---- get_frame ------------------------------------------------------------------------------
def get_frame(attempt: Optional[AttemptResult], phase: str, ws: Path, rnd: int) -> str:
    if attempt is None:
        return NO_ATTEMPT
    phase = _phase(attempt, phase)
    check, path = _check(attempt, phase), _frame(attempt, phase)
    if check is None and path is None:
        return f"No phase or frame named '{phase}'. Available: {_available(attempt)}."
    lines = [f"# {phase} on {attempt.env} (v{attempt.version})"]
    if check is not None:
        lines.append(f"**Check:** {_check_line(check)}")
    if path:
        lines.append(f"**Annotated frame:** {_localise(path, ws, f'round{rnd}_{attempt.env}_{phase}')}")
    else:
        lines.append("_no frame captured for this phase_")
    calls = _calls(attempt, check)
    if calls:
        lines.append("\n## Calls in this phase\n" + _trace_table(attempt, calls=calls))
    return "\n".join(lines) + "\n"


# ---- get_object_track -------------------------------------------------------------------------
def _held_label(scene: Optional[Scene], tip) -> Optional[str]:
    if scene is None or tip is None or not scene.objects:
        return None
    nearest = min(scene.objects, key=lambda o: math.hypot(o.x - tip[0], o.y - tip[1]))
    return nearest.label if math.hypot(nearest.x - tip[0], nearest.y - tip[1]) <= HOLD_RADIUS else None


def _cm(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1]) * 100


def get_object_track(attempt: Optional[AttemptResult], name: str) -> str:
    if attempt is None:
        return NO_ATTEMPT
    before, after = attempt.scene_before, attempt.scene_after
    name = (name or "").strip()
    found = None
    if name:
        found = (before.find(name) if before else None) or (after.find(name) if after else None)
    if found is None:
        seen_labels = sorted({o.label for s in (before, after) if s for o in s.objects})
        return f"No object matching '{name}'. Seen: {', '.join(seen_labels) or 'nothing'}."
    label = found.label
    start = before.find(label) if before else None
    origin = (start.x, start.y) if start else None
    rows: List[str] = []

    def moved(xy) -> str:
        return f"{_cm(xy, origin):.1f}" if origin and xy else "—"

    def seen(index: str, where: str, scene: Optional[Scene]) -> None:
        obj = scene.find(label) if scene else None
        if obj is None:
            rows.append(f"| {index} | {where} | not seen | — | — |")
            return
        hidden = " (hidden: last seen)" if not obj.visible else ""
        rows.append(f"| {index} | {where}{hidden} | {_fmt_xyz((obj.x, obj.y, obj.z))} | — | {moved((obj.x, obj.y))} |")

    def gripper(call: PrimitiveCall, event: str, where: str) -> None:
        rows.append(f"| #{call.index} | {event} | {where} | {_fmt_xyz(call.ee_after)} | {moved(call.ee_after)} |")

    seen("start", "scene before", before)
    last_scene, held, last_tip = before, False, None
    for call in attempt.trace:
        if call.name == "look" and isinstance(call.result, Scene):
            last_scene = call.result
            seen(f"#{call.index}", "seen by look()", call.result)
        elif call.name == "grasp" and call.result is True and not held:
            held = _held_label(last_scene, call.ee_after) == label
            if held:
                gripper(call, "grasped (matched by tip position)", "with gripper")
        elif held and call.name == "release":
            held = False
            gripper(call, "released", "under the gripper")
        elif held and call.name in ("move_to", "home", "align_over"):
            gripper(call, f"held: {call.name}" + ("" if call.ok else f" ✗ {call.error}"), "with gripper (inferred)")
        if held and call.ee_after is not None:
            last_tip = (call.index, call.ee_after)
    seen("end", "scene after", after)

    lines = [f"# Track of {label} on {attempt.env} (v{attempt.version})",
             "Camera rows are observed; 'with gripper' rows assume it moved with the tip.",
             "| # | event | object (x, y, z) | gripper tip | moved from start cm (xy) |", "|---|---|---|---|---|"]
    lines += rows
    end = after.find(label) if after else None
    if start and end:
        lines.append(f"\nStart → end: moved {_cm((end.x, end.y), origin):.1f} cm in xy, "
                     f"{(end.z - start.z) * 100:+.1f} cm in z.")
    if held and last_tip and end:
        gap = _cm((end.x, end.y), last_tip[1])
        if gap > HOLD_RADIUS * 100:
            lines.append(f"Never released, yet it ended {gap:.1f} cm (xy) from the tip's position after "
                         f"call #{last_tip[0]}: it left the gripper before then.")
    return "\n".join(lines) + "\n"


# ---- compare_with_success ---------------------------------------------------------------------
def compare_with_success(attempt: Optional[AttemptResult], reference: Optional[AttemptResult],
                         reference_kind: str, phase: str, ws: Path, rnd: int) -> str:
    if attempt is None:
        return NO_ATTEMPT
    if reference is None:
        return NO_REFERENCE
    phase = _phase(attempt, phase)
    act, exp = _check(attempt, phase), _check(reference, phase)
    have_phases = bool(attempt.phases or reference.phases)
    if have_phases and act is None and exp is None:
        return (f"No phase named '{phase}'. Failed attempt: {_available(attempt)}; "
                f"reference: {_available(reference)}.")
    lines = [f"# {phase or 'whole run'}: passed on {reference.env} (v{reference.version}, {reference_kind}) "
             f"vs failed on {attempt.env} (v{attempt.version})"]
    if have_phases:
        lines += ["| | expected (passed) | actual (failed) |", "|---|---|---|",
                  f"| check | {_check_line(exp)} | {_check_line(act)} |"]
        e_calls, a_calls = _calls(reference, exp), _calls(attempt, act)
    else:
        lines.append("_No phase checks recorded; comparing the whole trace._")
        e_calls, a_calls = reference.trace, attempt.trace
    if e_calls or a_calls:
        lines += ["\n## Call by call",
                  "| step | expected call | tip after | actual call | tip after | tips apart cm |",
                  "|---|---|---|---|---|---|"]
        for i in range(max(len(e_calls), len(a_calls))):
            e = e_calls[i] if i < len(e_calls) else None
            a = a_calls[i] if i < len(a_calls) else None
            e_tip, a_tip = (e.ee_after if e else None), (a.ee_after if a else None)
            apart = f"{math.dist(e_tip, a_tip) * 100:.1f}" if e_tip and a_tip else "—"
            lines.append(f"| {i + 1} | {_call_text(e)} | {_fmt_xyz(e_tip)} | {_call_text(a)} | "
                         f"{_fmt_xyz(a_tip)} | {apart} |")
    frames = [(who, run, _frame(run, phase)) for who, run in (("expected", reference), ("actual", attempt))]
    if any(path for _, _, path in frames):
        lines.append("\n## Frames")
        for who, run, path in frames:
            shown = _localise(path, ws, f"round{rnd}_{who}_{run.env}_{phase}") if path else "_none_"
            lines.append(f"- {who} ({run.env}): {shown}")
    if reference.scene_after is not None and attempt.scene_after is not None:
        lines.append("\n## Where objects ended\n"
                     + _scene_delta(reference.scene_after, attempt.scene_after, reference.env, attempt.env))
    return "\n".join(lines) + "\n"
