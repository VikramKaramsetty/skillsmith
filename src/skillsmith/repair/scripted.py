"""A stand-in author that replays fixed sources. For tests and `make demo-fake` only:
it lets the whole loop run with no Droid, no network and no API key."""

from __future__ import annotations

from typing import List

from skillsmith.repair.protocols import AuthorContext
from skillsmith.types import TaskSpec


class ScriptedAuthor:
    def __init__(self, drafts: List[str], repairs: List[str]):
        self.drafts = list(drafts)
        self.repairs = list(repairs)
        self.reports: List[str] = []  # what the loop sent, for assertions

    def draft(self, task: TaskSpec, ctx: AuthorContext) -> str:
        if not self.drafts:
            raise RuntimeError("ScriptedAuthor has no more drafts")
        return self.drafts.pop(0)

    def repair(self, task: TaskSpec, source: str, report_md: str, ctx: AuthorContext) -> str:
        self.reports.append(report_md)
        if not self.repairs:
            raise RuntimeError("ScriptedAuthor has no more repairs")
        return self.repairs.pop(0)
