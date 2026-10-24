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
    A wrong label is worse than UNKNOWN: a skill label below confidence 0.6 goes out as UNKNOWN.
    Built by `analysis.classify.classify` (WS4.5).
- `should_escalate(previous, current, last_change_type) -> bool`: True when the same class
  repeats after a "parameter" change (Droid's structured result), so the next evidence says
  "change the approach, not just a number".
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from skillsmith.analysis.classify import PerceptionGate, classify
from skillsmith.analysis.phases import segment_phases
from skillsmith.types import AttemptResult, Classification, PhaseCheck


def classifier_enabled(cfg) -> bool:
    """`analysis.classifier: true` in the config switches the product onto the classifier. Off by default
    until the lead enables it together with the ws5 tests that still expect the stub (see HANDOFF)."""
    part = cfg.get("analysis") if cfg is not None and hasattr(cfg, "get") else None
    return bool(part.get("classifier", False)) if part is not None else False


def analyse_attempt(attempt: AttemptResult, cfg, perception: Optional[PerceptionGate] = None,
                    ) -> Tuple[List[PhaseCheck], Optional[Classification]]:
    """The product entry point: `analyse` when the classifier is enabled, else the stub's
    NotImplementedError, which every caller already treats as "no analysis yet"."""
    if not classifier_enabled(cfg):
        raise NotImplementedError("WS4.5 classifier is built but off: set analysis.classifier: true")
    return analyse(attempt, cfg, perception)


def analyse(attempt: AttemptResult, cfg, perception: Optional[PerceptionGate] = None,
            ) -> Tuple[List[PhaseCheck], Optional[Classification]]:
    """Phases + classification, always. `perception`: `WorldModel.perception_ok()` taken right after
    the attempt, when the env has one (the runtime passes it). Rules and order: `analysis.classify`."""
    phases = segment_phases(attempt, cfg)
    return phases, classify(attempt, cfg, phases, perception)


def should_escalate(previous: Optional[Classification], current: Classification,
                    last_change_type: Optional[str]) -> bool:
    raise NotImplementedError("WS4.6")
