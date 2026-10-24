"""Cartesian safety envelope. See docs/SAFETY.md.

SafetyGuard runs BEFORE every motion (raises SafetyViolation, nothing moves).
audit_trace re-checks a finished trace (used for the sim gate report).
Joint limits and per-step clamps live lower down, in robotd (last line of defence).
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional

from skillsmith.types import PrimitiveCall, SafetyIssue, SafetyReport, SafetyViolation, Vec3


class SafetyGuard:
    def __init__(self, cfg):
        self.ws = cfg.workspace
        self.s = cfg.safety
        self.calls = 0

    def target_issues(self, p: Vec3, call_index: Optional[int] = None) -> List[SafetyIssue]:
        x, y, z = p
        issues = []
        for axis, v, (lo, hi) in (("x", x, self.ws.x), ("y", y, self.ws.y), ("z", z, self.ws.z)):
            if not lo <= v <= hi:
                issues.append(SafetyIssue(
                    "OUT_OF_BOX", f"{axis}={v:.3f} outside workspace [{lo}, {hi}]", call_index))
        if z < self.s.min_tip_z:
            issues.append(SafetyIssue(
                "TOO_LOW", f"z={z:.3f} below min tip height {self.s.min_tip_z}", call_index))
        r = math.hypot(x, y)
        if r > self.ws.max_reach:
            issues.append(SafetyIssue(
                "OUT_OF_REACH", f"radius {r:.3f} > max reach {self.ws.max_reach}", call_index))
        if r < self.ws.min_reach:
            issues.append(SafetyIssue(
                "TOO_CLOSE", f"radius {r:.3f} < min reach {self.ws.min_reach}", call_index))
        return issues

    def check_target(self, p: Vec3, call_index: Optional[int] = None) -> None:
        issues = self.target_issues(p, call_index)
        if issues:
            raise SafetyViolation("; ".join(f"[{i.code}] {i.message}" for i in issues))

    def clamp_speed(self, speed: Optional[float]) -> float:
        if speed is None:
            return float(self.s.default_tcp_speed)
        if speed <= 0:
            raise SafetyViolation(f"speed must be positive, got {speed}")
        return float(min(speed, self.s.max_tcp_speed))

    def count_call(self) -> int:
        self.calls += 1
        if self.calls > self.s.max_calls:
            raise SafetyViolation(
                f"[CALL_BUDGET] more than {self.s.max_calls} primitive calls in one attempt")
        return self.calls - 1


_ERROR_CODES = (
    ("Unreachable", "UNREACHABLE"),
    ("CollisionError", "COLLISION"),
    ("SafetyViolation", "ENVELOPE"),
)


def audit_trace(trace: Iterable[PrimitiveCall], cfg,
                extra: Iterable[SafetyIssue] = ()) -> SafetyReport:
    """Re-check every recorded call. `extra` carries backend findings (e.g. sim contacts)."""
    guard = SafetyGuard(cfg)
    issues: List[SafetyIssue] = list(extra)
    n = 0
    for call in trace:
        n += 1
        if call.name == "move_to":
            a = call.args
            issues += guard.target_issues((a["x"], a["y"], a["z"]), call.index)
        if call.error:
            for needle, code in _ERROR_CODES:
                if call.error.startswith(needle):
                    issues.append(SafetyIssue(code, call.error, call.index))
                    break
    if n > cfg.safety.max_calls:
        issues.append(SafetyIssue("CALL_BUDGET", f"{n} calls > {cfg.safety.max_calls}"))
    return SafetyReport(ok=not issues, issues=issues)
