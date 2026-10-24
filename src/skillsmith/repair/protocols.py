"""Plug points for the repair loop. See docs/INTERFACES.md §6."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol, Tuple

from skillsmith.types import AttemptResult, Scene, TaskSpec


@dataclass
class AuthorContext:
    run_id: str
    round: int
    scene: Optional[Scene]
    snapshot_path: Optional[str]
    library: Dict[str, str] = field(default_factory=dict)  # name -> source, read-only


@dataclass
class RepairContext(AuthorContext):
    """AuthorContext plus the run under repair, for Droid's evidence tools (WS5.4).
    Local adapter until the lead folds these fields into AuthorContext (INTERFACES §6);
    authors read them with getattr(ctx, "attempt", None)."""
    attempt: Optional[AttemptResult] = None   # the failed run (target attempt or sim gate)
    expected: Optional[AttemptResult] = None  # a run of the same skill that passed (sim gate)


class SkillAuthor(Protocol):
    """Writes skill code. DroidAuthor in production, ScriptedAuthor in tests."""

    def draft(self, task: TaskSpec, ctx: AuthorContext) -> str: ...

    def repair(self, task: TaskSpec, source: str, report_md: str, ctx: AuthorContext) -> str: ...


class Approver(Protocol):
    """The human gate before the real arm moves."""

    def request(self, run_id: str, summary: dict) -> Tuple[bool, str]:
        """Return (approved, note). A rejection note becomes the next failure report."""
