"""Failure analysis: phase checks + failure classification. OWNER: WS4 (WS4.4–WS4.6).
See docs/ARCHITECTURE.md "Failure analysis" and docs/INTERFACES.md §5.

Plain rules over measured signals, no AI. Runs right after every attempt (runtime._analyse),
in milliseconds. Its output leads Droid's evidence and the dashboard banner.

Contract:
- `analyse_attempt(attempt, cfg) -> (phases, classification)`
  * `phases`: list[PhaseCheck] in types.PHASES order, segmented from the trace
    (approach = moves before the first grasp; grasp; lift = first upward move while holding;
    carry = horizontal moves while holding; place = descent while holding; release; retreat),
    each with ok/detail (e.g. "lost blue cup during 16 cm sideways move").
    Built by `analysis.phases.segment_phases(attempt, cfg)` (WS4.4).
  * `classification`: None when attempt.ok, else a Classification with a key of
    types.FAILURE_CLASSES, its bucket, the first failing phase and call, a one-line summary
    WITH NUMBERS, a hint, and a confidence.
  * Confidence gating: if perception reported low confidence, marker drift or an object-count
    change, return PERCEPTION_UNCERTAIN / CAMERA_MOVED (environment bucket), never a skill class.
    A wrong label is worse than UNKNOWN.
- `should_escalate(previous, current, last_change_type) -> bool`: True when the same class
  repeats after a "parameter" change (Droid's structured result), so the next evidence says
  "change the approach, not just a number".
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from skillsmith.types import AttemptResult, Classification, PhaseCheck


def analyse_attempt(attempt: AttemptResult, cfg) -> Tuple[List[PhaseCheck], Optional[Classification]]:
    raise NotImplementedError("WS4.4/WS4.5: see docs/workstreams/WS4_runtime_safety_analysis.md")


def should_escalate(previous: Optional[Classification], current: Classification,
                    last_change_type: Optional[str]) -> bool:
    raise NotImplementedError("WS4.6")
