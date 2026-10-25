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


class WorldChanged(SkillError):
    """An object moved when nothing should have moved it (bumped, knocked over).
    Raised by the Robot before the next motion when the world model reports it."""


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
    visible: bool = True  # False = hidden (e.g. behind the arm); position is the last one seen
    top_width: Optional[float] = None  # width at the top for a tapered object; None = same as width

    @property
    def top(self) -> float:
        return self.z + self.height

    def width_at(self, z: float) -> float:
        """Width (diameter) at height z above the table: linear from `width` at the base to
        `top_width` at the top, clamped to the object's span."""
        if self.top_width is None or self.height <= 0:
            return self.width
        f = min(max((z - self.z) / self.height, 0.0), 1.0)
        return self.width + (self.top_width - self.width) * f

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
    phases: list = field(default_factory=list)  # list[PhaseCheck], from analysis.analyse_attempt
    classification: Optional[Classification] = None
    keyframes: dict = field(default_factory=dict)  # phase -> annotated image path
    held_at_stop: bool = False  # the arm still held an object when the attempt ended (real/sim arms)

    def reason(self) -> str:
        """One line on why the attempt failed (or 'ok'). The classification leads when present."""
        if self.ok:
            return "ok"
        if self.classification is not None:
            return f"{self.classification.cls}: {self.classification.summary}"
        if self.error:
            return self.error
        if not self.safety.ok:
            return self.safety.summary()
        if self.verify and not self.verify.ok:
            return f"task not achieved: {self.verify.detail}"
        return "unknown failure"


# ---- failure analysis (docs/ARCHITECTURE.md "Failure analysis") -------------------------
PHASES = ("approach", "grasp", "lift", "carry", "place", "release", "retreat")

BUCKET_SKILL = "skill"              # Droid fixes it in code
BUCKET_ENVIRONMENT = "environment"  # a human / calibration fixes it; Droid is NOT called
BUCKET_UNKNOWN = "unknown"          # Droid gets all evidence, flagged low confidence

FAILURE_CLASSES = {
    # skill bugs
    "KNOCKED_ON_APPROACH": BUCKET_SKILL, "GRASP_MISS": BUCKET_SKILL, "SLIP_IN_CARRY": BUCKET_SKILL,
    "COLLISION": BUCKET_SKILL, "NO_PICK": BUCKET_SKILL, "PLACE_OFF_TARGET": BUCKET_SKILL,
    "PLACE_WRONG_HEIGHT": BUCKET_SKILL, "DROPPED_FROM_HEIGHT": BUCKET_SKILL, "UNSAFE_TARGET": BUCKET_SKILL,
    "SKILL_ERROR": BUCKET_SKILL, "STATIC_REJECTED": BUCKET_SKILL, "HUMAN_REJECTED": BUCKET_SKILL,
    "TIMEOUT": BUCKET_SKILL,
    # not a code problem
    "OBJECT_NOT_FOUND": BUCKET_ENVIRONMENT, "OBJECT_OUT_OF_REACH": BUCKET_ENVIRONMENT,
    "CAMERA_MOVED": BUCKET_ENVIRONMENT, "PERCEPTION_UNCERTAIN": BUCKET_ENVIRONMENT,
    "CALIBRATION_DRIFT": BUCKET_ENVIRONMENT, "HARDWARE_FAULT": BUCKET_ENVIRONMENT,
    "AMBIGUOUS_TASK": BUCKET_ENVIRONMENT, "BUMPED_BY_SOMETHING_ELSE": BUCKET_ENVIRONMENT,
    # honest fallback
    "UNKNOWN": BUCKET_UNKNOWN,
}


@dataclass
class PhaseCheck:
    phase: str             # one of PHASES
    ok: bool
    detail: str            # "holding blue cup at z=0.18" / "lost blue cup during 16 cm move"
    start_index: int       # first PrimitiveCall index of the phase
    end_index: int
    frame_path: Optional[str] = None  # annotated key frame for this phase


@dataclass
class Classification:
    cls: str                       # a key of FAILURE_CLASSES
    bucket: str                    # BUCKET_*
    phase: Optional[str]           # first failing phase
    call_index: Optional[int]      # first failing PrimitiveCall
    summary: str                   # one line with numbers; leads Droid's evidence and the dashboard banner
    hint: str = ""                 # where to look ("grip height", "place z")
    confidence: float = 1.0        # < 0.6 → treat as UNKNOWN-ish; never route a guess as certain
    escalate: bool = False         # same class after a parameter-only change: change the approach
    evidence: dict = field(default_factory=dict)  # the numbers behind the label
