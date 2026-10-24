"""Phase segmentation of an attempt's trace, with one measured check per phase. WS4.4.

Plain rules over the recorded trace (tip before/after each call, grasp result, errors) and the
scenes before/after. No AI. The classifier (WS4.5) labels the failure from these checks.

Segmentation runs on the LAST pick cycle (the one where execution stopped, or the one the
success check judged); a cycle is the calls after the previous release-while-holding:
- approach: calls before the first grasp of the cycle
- grasp:    first grasp .. first grasp that held something (retries included)
- lift:     the leading run of upward moves while holding
- carry:    the moves while holding between lift and place
- place:    the trailing run of downward moves while holding
- release:  the release that let go of the object
- retreat:  every call after that release
Phases with no calls are left out. Calls that do not move (note, look) join whichever phase
they fall in, so the phases cover the cycle without gaps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from skillsmith.types import AttemptResult, PhaseCheck, PrimitiveCall, Scene, SceneObject, Vec3

MOVE_EPS = 0.005  # m; a call that moves the tip less than this did not move
DROP_TOL = 0.02   # m; an object that falls further than this after release was dropped


@dataclass
class _Span:
    phase: str
    lo: int  # positions in attempt.trace, inclusive
    hi: int


@dataclass
class _Hand:
    """What the gripper closed on: the object (if known) and the tip position at the grasp."""
    obj: Optional[SceneObject]
    tip: Optional[Vec3]


def segment_phases(attempt: AttemptResult, cfg) -> List[PhaseCheck]:
    """PhaseChecks in types.PHASES order for the last pick cycle; [] for an empty trace."""
    trace: List[PrimitiveCall] = list(attempt.trace)
    if not trace:
        return []
    spans = _spans(trace)
    stop = _stop_position(attempt, trace)
    hand = _hand(attempt, trace, spans)
    checks = []
    for span in spans:
        calls = trace[span.lo:span.hi + 1]
        ok, detail = _CHECKS[span.phase](calls, hand, attempt, cfg)
        if stop is not None and span.lo <= stop <= span.hi:
            ok, detail = False, _failure_detail(span.phase, calls, hand, attempt, trace[stop])
        checks.append(PhaseCheck(span.phase, ok, detail, calls[0].index, calls[-1].index))
    return checks


# ---- segmentation ---------------------------------------------------------------------------

def _spans(trace: Sequence[PrimitiveCall]) -> List[_Span]:
    start = _cycle_start(trace)
    end = len(trace) - 1
    grasps = [i for i in range(start, end + 1) if trace[i].name == "grasp"]
    if not grasps:
        return [_Span("approach", start, end)]
    first = grasps[0]
    spans = [_Span("approach", start, first - 1)] if first > start else []
    held_at = next((i for i in grasps if _held(trace[i])), None)
    if held_at is None:
        return spans + [_Span("grasp", first, end)]
    spans.append(_Span("grasp", first, held_at))
    release = next((i for i in range(held_at + 1, end + 1) if trace[i].name == "release"), None)
    spans += _holding_spans(trace, held_at + 1, (end + 1 if release is None else release) - 1)
    if release is not None:
        spans.append(_Span("release", release, release))
        if release < end:
            spans.append(_Span("retreat", release + 1, end))
    return spans


def _held(call: PrimitiveCall) -> bool:
    return call.name == "grasp" and call.ok and call.result is True


def _cycle_start(trace: Sequence[PrimitiveCall]) -> int:
    """First position of the last cycle that contains a grasp (0 when there is only one)."""
    starts, holding = [0], False
    for i, call in enumerate(trace):
        if call.name == "grasp":
            holding = _held(call)
        elif call.name == "release" and holding:
            holding = False
            starts.append(i + 1)
    return next((s for s in reversed(starts) if any(c.name == "grasp" for c in trace[s:])), 0)


def _holding_spans(trace: Sequence[PrimitiveCall], lo: int, hi: int) -> List[_Span]:
    """lift = leading upward run, place = trailing downward run, carry = what is between."""
    if lo > hi:
        return []
    lift_end = lo - 1
    for i in range(lo, hi + 1):
        kind = _direction(trace[i])
        if kind == "up":
            lift_end = i
        elif kind is not None:
            break
    place_start = hi + 1
    for i in range(hi, lift_end, -1):
        kind = _direction(trace[i])
        if kind == "down":
            place_start = i
        elif kind is not None:
            break
    spans = []
    if lift_end >= lo:
        spans.append(_Span("lift", lo, lift_end))
    if place_start > lift_end + 1:
        spans.append(_Span("carry", lift_end + 1, place_start - 1))
    if place_start <= hi:
        spans.append(_Span("place", place_start, hi))
    return spans


def _direction(call: PrimitiveCall) -> Optional[str]:
    """'up' / 'down' for mostly vertical moves, 'side' for sideways ones, None if it did not move."""
    if call.ee_before is None or call.ee_after is None:
        return None
    dxy = _xy(call.ee_before, call.ee_after)
    dz = call.ee_after[2] - call.ee_before[2]
    if max(dxy, abs(dz)) < MOVE_EPS:
        return None
    if abs(dz) >= dxy:
        return "up" if dz > 0 else "down"
    return "side"


def _stop_position(attempt: AttemptResult, trace: Sequence[PrimitiveCall]) -> Optional[int]:
    """Position of the first failing call; the last call when the skill raised between calls."""
    failing = next((i for i, c in enumerate(trace) if not c.ok), None)
    if failing is not None:
        return failing
    return len(trace) - 1 if attempt.error else None


# ---- the object in hand -----------------------------------------------------------------------

def _hand(attempt: AttemptResult, trace: Sequence[PrimitiveCall], spans: Sequence[_Span]) -> _Hand:
    """The object under the tip at the last grasp of the cycle, from the latest look() before it."""
    grasp = next((s for s in spans if s.phase == "grasp"), None)
    if grasp is None:
        return _Hand(None, None)
    tip = trace[grasp.hi].ee_after or trace[grasp.hi].ee_before
    scene = _scene_before(attempt, trace, grasp.lo)
    if tip is None or scene is None:
        return _Hand(None, tip)
    under = [o for o in scene.objects if _xy(tip, (o.x, o.y)) < max(o.width / 2, 0.02)]
    return _Hand(min(under, key=lambda o: _xy(tip, (o.x, o.y))) if under else None, tip)


def _scene_before(attempt: AttemptResult, trace: Sequence[PrimitiveCall], pos: int) -> Optional[Scene]:
    for call in reversed(trace[:pos]):
        if call.name == "look" and isinstance(call.result, Scene):
            return call.result
    return attempt.scene_before


def _after(attempt: AttemptResult, obj: Optional[SceneObject]) -> Optional[SceneObject]:
    if obj is None or attempt.scene_after is None:
        return None
    return next((o for o in attempt.scene_after.objects if o.id == obj.id), None)


# ---- per-phase checks: (calls, hand, attempt, cfg) -> (ok, detail) ----------------------------

def _check_approach(calls, hand: _Hand, attempt, cfg) -> Tuple[bool, str]:
    end, obj = calls[-1].ee_after, hand.obj
    if obj is None or end is None:
        return True, f"moved {_travel(calls)}, ended at {_fmt(end)}"
    dz = end[2] - obj.top
    return True, (f"ended {_cm(_xy(end, (obj.x, obj.y)))} off {obj.label} centre, "
                  f"{_cm(abs(dz))} {'above' if dz >= 0 else 'below'} its top")


def _check_grasp(calls, hand: _Hand, attempt, cfg) -> Tuple[bool, str]:
    tries = sum(1 for c in calls if c.name == "grasp")
    again = f" after {tries} tries" if tries > 1 else ""
    tip, obj = hand.tip or calls[-1].ee_after, hand.obj
    closed = f"closed at z={tip[2]:.3f}{again}" if tip else f"closed{again}"
    if not _held(calls[-1]):
        near = _nearest(attempt.scene_before, tip)
        where = f", nearest {near[0].label} {_cm(near[1])} away" if near else ""
        return False, f"{closed}, gripper empty{where}"
    if obj is None or tip is None or obj.height <= 0:
        return True, f"{closed}, holding"
    frac = (tip[2] - obj.z) / obj.height
    return True, f"{closed} = {frac:.0%} of {obj.label} height, holding"


def _check_lift(calls, hand: _Hand, attempt, cfg) -> Tuple[bool, str]:
    end = calls[-1].ee_after
    return True, f"lifted {_label(hand)} {_cm(_vertical(calls))} to z={end[2]:.3f}" if end else "lifted"


def _check_carry(calls, hand: _Hand, attempt, cfg) -> Tuple[bool, str]:
    return True, f"{_label(hand)} held through {_cm(_side(calls))} sideways move"


def _check_place(calls, hand: _Hand, attempt, cfg) -> Tuple[bool, str]:
    end = calls[-1].ee_after
    return True, f"lowered {_cm(_vertical(calls))} to z={end[2]:.3f}" if end else "lowered"


def _check_release(calls, hand: _Hand, attempt, cfg) -> Tuple[bool, str]:
    tip, obj, after = calls[-1].ee_before, hand.obj, _after(attempt, hand.obj)
    if obj is None or after is None or tip is None or hand.tip is None:
        return True, "opened"
    fall = tip[2] + (obj.z - hand.tip[2]) - after.z  # base height at release minus where it landed
    off = _xy(tip, (after.x, after.y))
    if fall > DROP_TOL:
        return False, f"dropped {obj.label} from {_cm(fall)} above where it landed"
    detail = f"opened at z={tip[2]:.3f}; {obj.label} settled {_cm(off)} from the release point"
    return off <= cfg.verify.xy_tol, detail


def _check_retreat(calls, hand: _Hand, attempt, cfg) -> Tuple[bool, str]:
    return True, f"moved {_travel(calls)} away, ended at {_fmt(calls[-1].ee_after)}"


_CHECKS = {"approach": _check_approach, "grasp": _check_grasp, "lift": _check_lift, "carry": _check_carry,
           "place": _check_place, "release": _check_release, "retreat": _check_retreat}


def _failure_detail(phase: str, calls, hand: _Hand, attempt: AttemptResult, stop: PrimitiveCall) -> str:
    """What went wrong in the phase where execution stopped, with the numbers we have."""
    error = stop.error or attempt.error or "failed"
    if phase not in ("lift", "carry", "place") or hand.obj is None:
        return error
    move = f"{_cm(_side(calls))} sideways" if phase == "carry" else f"{_cm(_vertical(calls))} vertical"
    after, tip = _after(attempt, hand.obj), calls[-1].ee_after
    if after is not None and tip is not None:
        gap = _xy(tip, (after.x, after.y))
        if gap > max(hand.obj.width / 2, 0.02):
            return f"lost {hand.obj.label} during {move} move ({_cm(gap)} from the gripper)"
    return f"{error} during {move} move with {hand.obj.label}"


# ---- numbers --------------------------------------------------------------------------------

def _label(hand: _Hand) -> str:
    return hand.obj.label if hand.obj else "object"


def _moves(calls) -> List[Tuple[Vec3, Vec3]]:
    return [(c.ee_before, c.ee_after) for c in calls if c.ee_before and c.ee_after]


def _xy(a: Sequence[float], b: Sequence[float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _side(calls) -> float:
    return sum(_xy(a, b) for a, b in _moves(calls))


def _vertical(calls) -> float:
    return sum(abs(b[2] - a[2]) for a, b in _moves(calls))


def _travel(calls) -> str:
    return _cm(sum(math.dist(a, b) for a, b in _moves(calls)))


def _nearest(scene: Optional[Scene], tip: Optional[Vec3]) -> Optional[Tuple[SceneObject, float]]:
    if scene is None or tip is None or not scene.objects:
        return None
    obj = min(scene.objects, key=lambda o: _xy(tip, (o.x, o.y)))
    return obj, _xy(tip, (obj.x, obj.y))


def _cm(metres: float) -> str:
    return f"{metres * 100:.1f} cm"


def _fmt(p: Optional[Vec3]) -> str:
    return f"({p[0]:.3f}, {p[1]:.3f}, {p[2]:.3f})" if p else "?"
