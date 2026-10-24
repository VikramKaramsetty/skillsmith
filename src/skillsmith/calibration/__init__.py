"""Self-calibration. OWNER: WS7 (docs/workstreams/WS7_library_calibration.md).

Contract:
- `fit_offset(commanded, measured) -> (dx, dy)`: least-squares xy offset in metres from test grabs
  (commanded tip positions vs where the camera saw the object end up).
- `OffsetKinematics(inner, dx, dy)`: wraps any `Kinematics`; `ik()` shifts the target by (-dx, -dy)
  and `fk()` adds (dx, dy) back, so `fk(ik(p)) == p` and fk reports where the tip really is.
  The shared contract does not change.
- Offsets are bounded: |(dx, dy)| above `MAX_OFFSET` is not a calibration error (a bumped camera,
  a moved base) and is refused with ValueError, for a human to look at.
- Offsets live in `calibration/offsets.json` ({"dx": 0.004, "dy": -0.002}), gitignored, and are
  loaded by the orchestrator when present. `apply_offsets(kin, bus=bus)` emits a `note` with
  dx, dy, path (or `error` with `where: "calibration"`) so the dashboard shows it.

A consistent offset (every place lands 1.5 cm left) is calibration, never a number hard-coded in
a skill. `repeated_offset` says whether the residuals really are one shared shift (small spread)
or scatter that calibration cannot fix.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple

from skillsmith.config import REPO_ROOT
from skillsmith.types import Vec3

if TYPE_CHECKING:
    from skillsmith.events import EventBus

OFFSETS_PATH = REPO_ROOT / "calibration" / "offsets.json"
MAX_SPREAD = 0.005  # m; residual scatter above this is not one repeated offset
MAX_OFFSET = 0.03  # m; a larger shift is a moved camera or base, not calibration
# §7 has no calibration event yet, so this is a `note` (text + dx, dy, path); proposed: "calibration.applied"
APPLIED_EVENT = "note"


def check_offset(dx: float, dy: float, max_offset: float = MAX_OFFSET) -> Tuple[float, float]:
    """(dx, dy) as floats if finite and within `max_offset` metres of zero, else ValueError."""
    dx, dy = float(dx), float(dy)
    if not (math.isfinite(dx) and math.isfinite(dy)):
        raise ValueError(f"calibration offset ({dx}, {dy}) is not finite")
    size = math.hypot(dx, dy)
    if size > max_offset:
        raise ValueError(
            f"calibration offset ({dx:.4f}, {dy:.4f}) is {size * 100:.1f} cm, over the "
            f"{max_offset * 100:.1f} cm bound: check the camera and the arm base, do not calibrate it away"
        )
    return dx, dy


def _pairs(commanded: Sequence[Sequence[float]], measured: Sequence[Sequence[float]]):
    if len(commanded) != len(measured):
        raise ValueError(f"fit_offset: {len(commanded)} commanded vs {len(measured)} measured points")
    if not commanded:
        raise ValueError("fit_offset: need at least one commanded/measured pair")
    return list(zip(commanded, measured))


def fit_offset(commanded: Sequence[Sequence[float]], measured: Sequence[Sequence[float]]) -> Tuple[float, float]:
    """Least-squares xy shift (dx, dy) in metres such that measured ≈ commanded + (dx, dy).

    For a pure translation the least-squares solution is the mean residual. z is ignored.
    """
    pairs = _pairs(commanded, measured)
    n = len(pairs)
    dx = sum(m[0] - c[0] for c, m in pairs) / n
    dy = sum(m[1] - c[1] for c, m in pairs) / n
    return (dx, dy)


def offset_spread(commanded: Sequence[Sequence[float]], measured: Sequence[Sequence[float]]) -> float:
    """RMS distance (m) of each residual from the fitted offset: how far from 'one shared shift'."""
    dx, dy = fit_offset(commanded, measured)
    pairs = _pairs(commanded, measured)
    sq = [(m[0] - c[0] - dx) ** 2 + (m[1] - c[1] - dy) ** 2 for c, m in pairs]
    return math.sqrt(sum(sq) / len(sq))


def repeated_offset(
    commanded: Sequence[Sequence[float]],
    measured: Sequence[Sequence[float]],
    max_spread: float = MAX_SPREAD,
    min_points: int = 3,
) -> Optional[Tuple[float, float]]:
    """The fitted (dx, dy) if at least `min_points` residuals agree within `max_spread`, else None."""
    if len(commanded) < min_points:
        return None
    if offset_spread(commanded, measured) > max_spread:
        return None
    return fit_offset(commanded, measured)


class OffsetKinematics:
    """Wraps a `Kinematics` so the arm lands where it was asked despite a known xy offset.

    If commanding p lands at p + (dx, dy), `ik(target)` solves for target - (dx, dy) and
    `fk()` adds (dx, dy) to the inner model, so it reports where the tip really is and
    `fk(ik(p)) == p`. Offsets over `max_offset` raise ValueError.
    """

    def __init__(self, inner, dx: float, dy: float, max_offset: float = MAX_OFFSET):
        self.inner = inner
        self.dx, self.dy = check_offset(dx, dy, max_offset)

    def fk(self, joints: Sequence[float]) -> Vec3:
        x, y, z = self.inner.fk(joints)
        return (x + self.dx, y + self.dy, z)

    def ik(self, target: Vec3, seed: Sequence[float]) -> Optional[List[float]]:
        shifted = (target[0] - self.dx, target[1] - self.dy, target[2])
        return self.inner.ik(shifted, seed)

    def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if callable(close):
            close()


def load_offsets(path: Path = OFFSETS_PATH) -> Optional[Tuple[float, float]]:
    """(dx, dy) from offsets.json, or None when the file is absent.

    Malformed files and offsets over `MAX_OFFSET` raise ValueError.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return check_offset(data["dx"], data["dy"])
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"bad calibration offsets in {path}: {e}") from e


def save_offsets(dx: float, dy: float, path: Path = OFFSETS_PATH) -> Path:
    """Write offsets.json; refuses (ValueError) offsets over `MAX_OFFSET` before touching the file."""
    dx, dy = check_offset(dx, dy)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dx": round(dx, 5), "dy": round(dy, 5)}) + "\n")
    return path


def apply_offsets(kin, path: Path = OFFSETS_PATH, bus: Optional[EventBus] = None):
    """`kin` wrapped in OffsetKinematics when offsets.json is present, else `kin` unchanged.

    With a `bus`, emits `APPLIED_EVENT` (text, dx, dy, path) when it wraps, and
    `error` (where "calibration") before re-raising a bad or out-of-bounds file.
    """
    try:
        offsets = load_offsets(path)
    except ValueError as e:
        if bus is not None:
            bus.emit("error", where="calibration", message=str(e))
        raise
    if offsets is None:
        return kin
    if bus is not None:
        dx, dy = offsets
        text = f"calibration applied: dx {dx * 100:+.1f} cm, dy {dy * 100:+.1f} cm (from {path})"
        bus.emit(APPLIED_EVENT, text=text, dx=dx, dy=dy, path=str(path))
    return OffsetKinematics(kin, *offsets)
