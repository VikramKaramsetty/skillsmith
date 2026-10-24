"""Self-calibration. OWNER: WS7 (docs/workstreams/WS7_library_calibration.md).

Contract:
- `fit_offset(commanded, measured) -> (dx, dy)`: least-squares xy offset in metres from test grabs
  (commanded tip positions vs where the camera saw the object end up). WS7.1.
- `OffsetKinematics(inner, dx, dy)`: wraps any `Kinematics`; `ik()` shifts the target by (-dx, -dy),
  `fk()` is unchanged. The shared contract does not change. WS7.2.
- Offsets live in `calibration/offsets.json` ({"dx": 0.004, "dy": -0.002}), gitignored, and are
  loaded by the orchestrator when present.
"""

from __future__ import annotations


def fit_offset(commanded, measured):
    raise NotImplementedError("WS7.1: see docs/workstreams/WS7_library_calibration.md")


class OffsetKinematics:
    def __init__(self, inner, dx: float, dy: float):
        raise NotImplementedError("WS7.2")
