"""Plug points for the repair loop. See docs/INTERFACES.md §6."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol, Tuple

from skillsmith.types import Scene, TaskSpec


@dataclass
class AuthorContext:
    run_id: str
    round: int
    scene: Optional[Scene]
    snapshot_path: Optional[str]
    library: Dict[str, str] = field(default_factory=dict)  # name -> source, read-only


class SkillAuthor(Protocol):
    """Writes skill code. DroidAuthor in production, ScriptedAuthor in tests."""

    def draft(self, task: TaskSpec, ctx: AuthorContext) -> str: ...

    def repair(self, task: TaskSpec, source: str, report_md: str, ctx: AuthorContext) -> str: ...


class Approver(Protocol):
    """The human gate before the real arm moves."""

    def request(self, run_id: str, summary: dict) -> Tuple[bool, str]:
        """Return (approved, note). A rejection note becomes the next failure report."""
