"""Shared data types. This file is a contract: see docs/INTERFACES.md §1.

Units: metres, seconds, joint degrees, gripper 0..100 (0 = closed).
Frame: robot base, x forward, y left, z up, origin on the table under shoulder_pan.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Tuple

Vec3 = Tuple[float, float, float]


class SkillError(Exception):
    """A readable, skill-level failure. Its message goes into Droid's failure report."""


class SafetyViolation(SkillError):
    """Raised by the safety guard BEFORE any motion happens."""


class Unreachable(SafetyViolation):
    """IK found no solution for a target."""


class CollisionError(SkillError):
    """Sim or fake world detected unintended contact."""


@dataclass(frozen=True)
class SceneObject:
    id: str
    label: str
    color: str
    kind: str
    x: float
    y: float
    z: float  # height of the object's base above the table
    height: float
    width: float
    confidence: float = 1.0

    @property
    def top(self) -> float:
        return self.z + self.height

    def xy_dist(self, other: SceneObject) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)


@dataclass(frozen=True)
class Scene:
    objects: Tuple[SceneObject, ...]
    t: float
    source: str
    image_path: Optional[str] = None

    def find(self, query: str) -> Optional[SceneObject]:
        """Match by id, then exact label, then 'color kind', then any label containing query."""
        q = query.strip().lower()
        for match in (
            lambda o: o.id.lower() == q,
            lambda o: o.label.lower() == q,
            lambda o: f"{o.color} {o.kind}".lower() == q,
            lambda o: q in o.label.lower(),
        ):
            hits = [o for o in self.objects if match(o)]
            if hits:
                return max(hits, key=lambda o: o.confidence)
        return None

    def require(self, query: str) -> SceneObject:
        obj = self.find(query)
        if obj is None:
            seen = ", ".join(o.label for o in self.objects) or "nothing"
            raise SkillError(f"'{query}' not found in scene (saw: {seen})")
        return obj

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> Scene:
        return Scene(
            objects=tuple(SceneObject(**o) for o in d["objects"]),
            t=d["t"],
            source=d["source"],
            image_path=d.get("image_path"),
        )


@dataclass
class PrimitiveCall:
    index: int
    name: str
    args: dict
    t: float
    ee_before: Optional[Vec3] = None
    ee_after: Optional[Vec3] = None
    ok: bool = True
    result: Any = None
    error: Optional[str] = None


@dataclass
class SafetyIssue:
    code: str
    message: str
    call_index: Optional[int] = None


@dataclass
class SafetyReport:
    ok: bool
    issues: list = field(default_factory=list)  # list[SafetyIssue]

    def summary(self) -> str:
        if self.ok:
            return "all safety checks passed"
        return "; ".join(f"[{i.code}] {i.message}" for i in self.issues)


@dataclass
class VerifyResult:
    ok: bool
    detail: str


@dataclass
class TaskSpec:
    text: str
    skill: str
    params: dict
    success: dict


@dataclass
class AttemptResult:
    ok: bool
    env: str
    skill: str
    version: int
    trace: list  # list[PrimitiveCall]
    error: Optional[str]
    verify: Optional[VerifyResult]
    safety: SafetyReport
    scene_before: Optional[Scene]
    scene_after: Optional[Scene]
    duration_s: float
    video_path: Optional[str] = None

    def reason(self) -> str:
        """One line on why the attempt failed (or 'ok')."""
        if self.ok:
            return "ok"
        if self.error:
            return self.error
        if not self.safety.ok:
            return self.safety.summary()
        if self.verify and not self.verify.ok:
            return f"task not achieved: {self.verify.detail}"
        return "unknown failure"
