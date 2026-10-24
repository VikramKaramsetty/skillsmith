"""Lessons in action (WS5.8): pick the past lessons that fit this failure, and say why.

skills/lessons.md holds one line per lesson (droid_tools.append_lesson writes them):

    - 2026-10-24 (stack the blue cup on the red cup; skill stack; SLIP_IN_CARRY in carry): Grip cups ...

`lesson_context()` builds the part in brackets, so a later failure can match a lesson by its
failure class, skill and phase. `select()` scores every lesson against the failure being
repaired and returns the relevant ones first (then the most recent, so Droid always sees the
newest rules). The loop puts the picks into Droid's evidence, marks each with why it was
chosen, and emits a `repair.lessons` event so the dashboard shows which lessons were used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from skillsmith.types import AttemptResult, TaskSpec

MAX_PICKS = 8  # under report.MAX_LESSONS, so every pick reaches Droid

_LINE = re.compile(r"^- (?P<date>\S+) \((?P<context>.*?)\): (?P<text>.+)$")
_CLASS = re.compile(r"\b[A-Z][A-Z]+(?:_[A-Z]+)+\b|\bTIMEOUT\b|\bCOLLISION\b|\bUNKNOWN\b")
_WORD = re.compile(r"[a-z][a-z]+")
_STOP = frozenset(
    "the and for with from into onto then than this that when while after before only once each "
    "its are was were has have had not but use used skill skills line raise error skillerror py "
    "task failed fail failure ok cm mm deg your you all any one two more less very".split())


@dataclass
class Lesson:
    line: str     # the stored "- ..." line
    text: str     # the rule itself
    context: str  # what was in brackets: task, skill, class, phase
    index: int    # position in the file (higher = newer)


@dataclass
class Pick:
    lesson: Lesson
    score: int
    why: List[str] = field(default_factory=list)

    @property
    def relevant(self) -> bool:
        return self.score > 0

    def evidence_line(self) -> str:
        """The lesson as Droid sees it, with the reason it was chosen."""
        reason = "; ".join(self.why) if self.why else "recent"
        return f"{self.lesson.line} _(chosen: {reason})_"

    def to_dict(self) -> dict:
        return {"lesson": self.lesson.text, "context": self.lesson.context, "score": self.score,
                "why": self.why or ["recent"]}


def parse(text: Optional[str]) -> List[Lesson]:
    lessons = []
    for line in (text or "").splitlines():
        if not line.startswith("- "):
            continue
        m = _LINE.match(line)
        context, body = (m.group("context"), m.group("text")) if m else ("", line[2:])
        lessons.append(Lesson(line, body.strip(), context, len(lessons)))
    return lessons


def lesson_context(task: TaskSpec, attempt: Optional[AttemptResult] = None) -> str:
    """What append_lesson stores in brackets: task text, skill, failure class and phase."""
    parts = [task.text, f"skill {task.skill}"]
    c = attempt.classification if attempt is not None else None
    if c is not None and c.cls != "UNKNOWN":
        parts.append(f"{c.cls} in {c.phase}" if c.phase else c.cls)
    return "; ".join(parts)


def select(lessons: List[Lesson], task: TaskSpec, attempt: Optional[AttemptResult],
           limit: int = MAX_PICKS) -> List[Pick]:
    """Relevant lessons first (highest score, then newest), topped up with the newest others."""
    scored = [_score(lesson, task, attempt) for lesson in lessons]
    relevant = sorted((p for p in scored if p.relevant), key=lambda p: (-p.score, -p.lesson.index))
    rest = sorted((p for p in scored if not p.relevant), key=lambda p: -p.lesson.index)
    return (relevant + rest)[:limit]


def _score(lesson: Lesson, task: TaskSpec, attempt: Optional[AttemptResult]) -> Pick:
    pick = Pick(lesson, 0)
    c = attempt.classification if attempt is not None else None
    if c is not None and c.cls != "UNKNOWN" and c.cls in _CLASS.findall(lesson.line):
        pick.score += 5
        pick.why.append(f"same failure class {c.cls}")
    if _has_word(lesson.context, task.skill):
        pick.score += 3
        pick.why.append(f"same skill {task.skill}")
    if c is not None and c.phase and _has_word(lesson.line, c.phase):
        pick.score += 1
        pick.why.append(f"same phase {c.phase}")
    shared = sorted(_words(_failure_text(task, attempt)) & _words(lesson.line) - {task.skill.lower()})
    if shared:
        pick.score += min(len(shared), 3)
        pick.why.append("shared words: " + ", ".join(shared[:5]))
    return pick


def _failure_text(task: TaskSpec, attempt: Optional[AttemptResult]) -> str:
    parts = [task.text, " ".join(str(v) for v in task.params.values())]
    if attempt is not None:
        parts.append(attempt.reason())
        c = attempt.classification
        if c is not None:
            parts += [c.summary, c.hint]
    return " ".join(parts)


def _words(text: str) -> set:
    return {_stem(w) for w in _WORD.findall(text.lower()) if len(w) > 2 and w not in _STOP}


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _has_word(text: str, word: str) -> bool:
    return bool(word) and re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE) is not None
