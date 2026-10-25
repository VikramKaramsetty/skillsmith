"""Droid's evidence tools (WS5.4): pull more evidence on demand, like a human scrubbing video.

- `get_frame(phase)`: the phase check, its calls and what its annotated key frame shows, as text:
  objects with positions, gripper tip, planned target and the error arrow in cm. droid-sdk 0.5
  tool results are text only (its MCP server wraps every return in one TextContent), and
  read_file refuses images, so the frame reaches Droid as this description. The image itself
  is copied into the workspace's frames/ for the dashboard and humans.
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
from typing import List, Optional, Sequence, Tuple

from skillsmith.repair.report import CROP_KEYS, _fmt_xyz, _ordered_frames, _scene_delta, _trace_table
from skillsmith.repair.workspace import under_roots
from skillsmith.types import AttemptResult, PhaseCheck, PrimitiveCall, Scene, Vec3

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


def _localise(path: str, ws: Path, tag: str, roots: Optional[Sequence[Path]] = None) -> str:
    """Copy a frame into the workspace's frames/ (for humans); say so if it is missing. With `roots`
    (runs_dir, WS5.11), a path outside them is never copied."""
    if roots is not None and not under_roots(path, roots):
        return f"`{Path(path).name}` _(not copied: outside runs_dir)_"
    src = Path(path)
    if not src.is_file():
        return f"`{path}` _(missing: the file was not saved)_"
    dest = ws / "frames" / f"{tag}_{src.name}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest)
    return f"`frames/{dest.name}` (image; described here as text)"


def _check_line(check: Optional[PhaseCheck]) -> str:
    if check is None:
        return "— (not reached)"
    return f"{'✓' if check.ok else '✗'} {check.detail} (calls #{check.start_index}–#{check.end_index})"


def _call_text(call: Optional[PrimitiveCall]) -> str:
    if call is None:
        return "—"
    args = ", ".join(f"{v:.3f}" if isinstance(v, float) else str(v) for v in call.args.values() if v is not None)
    return f"#{call.index} {call.name}({args}){'' if call.ok else ' ✗'}"


# ---- what a frame shows, as text ---------------------------------------------------------------
def _scene_at(attempt: AttemptResult, phase: str, check: Optional[PhaseCheck]) -> Tuple[Optional[Scene], str]:
    """The best estimate of the table at the end of a phase, and where it came from."""
    if phase == "crop_before" or (check is None and phase != "crop_after"):
        return attempt.scene_before, "the scene before the attempt"
    if check is None:
        return attempt.scene_after, "the scene after the attempt"
    looks = [c for c in attempt.trace if c.name == "look" and isinstance(c.result, Scene) and c.index <= check.end_index]
    last_index = attempt.trace[-1].index if attempt.trace else -1
    if check.end_index >= last_index and attempt.scene_after is not None:
        return attempt.scene_after, "the scene after the attempt (this phase ends the run)"
    if looks:
        return looks[-1].result, f"the look() at call #{looks[-1].index}"
    return attempt.scene_before, "the scene before the attempt (no look() before this phase ended)"


def _target(calls: List[PrimitiveCall]) -> Optional[Tuple[PrimitiveCall, Vec3]]:
    """The last move_to in the phase: where the skill meant the tip to be (the crosshair)."""
    for call in reversed(calls):
        xyz = [call.args.get(k) for k in ("x", "y", "z")]
        if call.name == "move_to" and all(isinstance(v, (int, float)) for v in xyz):
            return call, (float(xyz[0]), float(xyz[1]), float(xyz[2]))
    return None


def _tip(calls: List[PrimitiveCall]) -> Optional[Tuple[PrimitiveCall, Vec3]]:
    for call in reversed(calls):
        if call.ee_after is not None:
            return call, tuple(call.ee_after)
    return None


def _object_rows(scene: Scene, tip: Optional[Vec3]) -> List[str]:
    rows = ["| object | x | y | z (base) | top | width | xy from tip cm |", "|---|---|---|---|---|---|---|"]
    for o in scene.objects:
        hidden = " (hidden: last seen)" if not o.visible else ""
        apart = f"{_cm((o.x, o.y), tip):.1f}" if tip else "—"
        rows.append(f"| {o.label}{hidden} | {o.x:.3f} | {o.y:.3f} | {o.z:.3f} | {o.top:.3f} | {o.width:.3f} | {apart} |")
    return rows


def _arrow(tip: Vec3, target: Vec3) -> str:
    d = [(t - p) * 100 for p, t in zip(tip, target)]
    return (f"dx {d[0]:+.1f}, dy {d[1]:+.1f}, dz {d[2]:+.1f} cm "
            f"({math.hypot(d[0], d[1]):.1f} cm in xy, {math.dist(tip, target) * 100:.1f} cm in 3D)")


def _nearest(scene: Optional[Scene], tip: Optional[Vec3]) -> Optional[str]:
    if scene is None or tip is None or not scene.objects:
        return None
    o = min(scene.objects, key=lambda obj: _cm((obj.x, obj.y), tip))
    return (f"{o.label}, {_cm((o.x, o.y), tip):.1f} cm away in xy; tip {(tip[2] - o.top) * 100:+.1f} cm "
            f"from its top, {(tip[2] - o.z) * 100:+.1f} cm from its base")


def describe_frame(attempt: AttemptResult, phase: str, check: Optional[PhaseCheck]) -> List[str]:
    """The annotated frame's overlays as text: boxes (objects), tip dot, target crosshair, error arrow."""
    calls = _calls(attempt, check)
    tip, target = _tip(calls), _target(calls)
    scene, source = _scene_at(attempt, phase, check)
    tip_xyz = tip[1] if tip else None
    lines = ["\n## What the frame shows (as text)"]
    if scene is not None and scene.objects:
        lines += [f"Objects, from {source}:"] + _object_rows(scene, tip_xyz)
    else:
        lines.append(f"No objects detected in {source}.")
    if tip:
        lines.append(f"- Gripper tip (after call #{tip[0].index} {tip[0].name}): {_fmt_xyz(tip_xyz)}")
    if target:
        lines.append(f"- Planned target (call #{target[0].index} move_to): {_fmt_xyz(target[1])}")
    if tip and target:
        lines.append(f"- Error arrow, tip → target: {_arrow(tip_xyz, target[1])}")
    near = _nearest(scene, tip_xyz)
    if near:
        lines.append(f"- Nearest object to the tip: {near}")
    return lines


def _frame_summary(attempt: AttemptResult, phase: str) -> str:
    """One line for compare_with_success: where the tip ended and the nearest object."""
    check = _check(attempt, phase)
    tip = _tip(_calls(attempt, check))
    if tip is None:
        return "no tip position recorded"
    near = _nearest(_scene_at(attempt, phase, check)[0], tip[1])
    return f"tip {_fmt_xyz(tip[1])}" + (f"; nearest object: {near}" if near else "")


# ---- get_frame ------------------------------------------------------------------------------
def get_frame(attempt: Optional[AttemptResult], phase: str, ws: Path, rnd: int,
              roots: Optional[Sequence[Path]] = None) -> str:
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
        lines.append(f"**Annotated frame:** {_localise(path, ws, f'round{rnd}_{attempt.env}_{phase}', roots)}")
    else:
        lines.append("_no frame captured for this phase_")
    lines += describe_frame(attempt, phase, check)
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
        elif held and call.name in ("move_to", "home", "align_over", "approach_side"):
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
                         reference_kind: str, phase: str, ws: Path, rnd: int,
                         roots: Optional[Sequence[Path]] = None) -> str:
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
            shown = _localise(path, ws, f"round{rnd}_{who}_{run.env}_{phase}", roots) if path else "_none_"
            lines.append(f"- {who} ({run.env}): {shown}; shows {_frame_summary(run, phase)}")
    if reference.scene_after is not None and attempt.scene_after is not None:
        lines.append("\n## Where objects ended\n"
                     + _scene_delta(reference.scene_after, attempt.scene_after, reference.env, attempt.env))
    return "\n".join(lines) + "\n"
