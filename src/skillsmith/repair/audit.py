"""Audit trail (WS5.8): one record, and optionally one git commit, per saved skill version.

Every version the loop saves gets `<audit_dir>/<skill>/v<N>.json` (for tools) and `v<N>.md`
(for people): what failed, the lessons Droid was given, Droid's diagnosis and change, the sim
and safety result, who approved it, and the diff. `audit_dir` defaults to the folder next to
the library (skills/audit/).

`commit()` then records the skill, registry, lessons and audit record as one local git commit
(never pushed, hooks skipped, only those paths, other staged work untouched). `repair.audit_commit`:
`true` always (when the library is in a git work tree), `false` never, `"auto"` (default) only
when the target is not the fake world, so tests and `make demo-fake` never commit.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

GIT_TIMEOUT_S = 20


@dataclass
class RoundRecord:
    """One repair round: what triggered it and what the author said it did."""
    round: int
    trigger: str                            # first line of the report the author got
    failure_class: Optional[str] = None
    phase: Optional[str] = None
    lessons: List[str] = field(default_factory=list)  # lessons in this round's evidence
    diagnosis: Optional[str] = None         # from the author's structured result (Droid)
    change: Optional[str] = None
    change_type: Optional[str] = None
    confidence: Optional[float] = None
    lesson: Optional[str] = None            # the lesson the author wrote this round


@dataclass
class AuditRecord:
    run_id: str
    skill: str
    version: int
    task: str
    target: str
    author: str
    approver: str
    approval_note: str
    sim: str
    safety: str
    rounds: List[RoundRecord]
    diff: str
    saved_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))

    def to_dict(self) -> dict:
        return asdict(self)

    def subject(self) -> str:
        n = len(self.rounds)
        how = f"after {n} repair round{'s' if n != 1 else ''}" if n else "new skill, no repair"
        return f"skillsmith: save {self.skill} v{self.version} ({how}, run {self.run_id})"

    def body(self) -> str:
        """Plain text: the git commit body and the core of the markdown record."""
        lines = [f"Task: {self.task}", f"Target: {self.target}", f"Author: {self.author}"]
        for r in self.rounds:
            failed = f"{r.failure_class} in {r.phase}" if r.failure_class and r.phase else r.failure_class
            lines += ["", f"Round {r.round}: {r.trigger}"]
            if failed:
                lines.append(f"  Failure class: {failed}")
            lines.append(f"  Lessons in the evidence: {len(r.lessons)}")
            lines += [f"    - {text}" for text in r.lessons]
            if r.diagnosis:
                lines.append(f"  Droid's diagnosis: {r.diagnosis}")
            if r.change:
                lines.append(f"  Droid's change ({r.change_type or 'unlabelled'}): {r.change}")
            if r.lesson:
                lines.append(f"  Lesson written: {r.lesson}")
        lines += ["", f"Sim: {self.sim}", f"Safety: {self.safety}",
                  f"Approved by: {self.approver}" + (f" ({self.approval_note})" if self.approval_note else "")]
        return "\n".join(lines)

    def markdown(self) -> str:
        return (f"# {self.skill} v{self.version}\n\n{self.subject()}\n\n```\n{self.body()}\n```\n\n"
                f"## Diff\n\n```diff\n{self.diff or '(no change)'}\n```\n")


def audit_dir(cfg, library_dir: Path) -> Path:
    configured = cfg.repair.get("audit_dir")
    return cfg.path(configured) if configured else Path(library_dir).parent / "audit"


def write(record: AuditRecord, directory: Path) -> List[Path]:
    """Write v<N>.json and v<N>.md under directory/<skill>/; returns both paths."""
    folder = Path(directory) / record.skill
    folder.mkdir(parents=True, exist_ok=True)
    js, md = folder / f"v{record.version}.json", folder / f"v{record.version}.md"
    js.write_text(json.dumps(record.to_dict(), indent=2) + "\n")
    md.write_text(record.markdown())
    return [js, md]


def should_commit(mode, target_env: str) -> bool:
    if isinstance(mode, str):
        mode = mode.strip().lower()
        if mode == "auto":
            return target_env != "fake"
        return mode in ("true", "yes", "on", "1")
    return bool(mode)


def commit(paths: List[Path], record: AuditRecord) -> Optional[str]:
    """Commit exactly `paths` (those inside the work tree and not ignored). Returns the sha, or
    None when the files are not in a git work tree. Raises RuntimeError if git fails."""
    existing = [Path(p).resolve() for p in paths if Path(p).exists()]
    if not existing:
        return None
    top = _git(existing[0].parent, "rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0:
        return None
    root = Path(top.stdout.strip()).resolve()
    rel = [str(p.relative_to(root)) for p in existing if _inside(p, root)]
    rel = [p for p in rel if _git(root, "check-ignore", "-q", "--", p, check=False).returncode != 0]
    if not rel:
        return None
    _git(root, "add", "--", *rel)
    if _git(root, "diff", "--cached", "--quiet", "--", *rel, check=False).returncode == 0:
        return None  # nothing new to record
    _git(root, *_identity(root), "commit", "--no-verify", "--only", "-q",
         "-m", record.subject(), "-m", record.body(), "--", *rel)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _identity(root: Path) -> List[str]:
    """Commit as the configured user; fall back to a Skillsmith identity on a bare machine."""
    if _git(root, "config", "user.email", check=False).stdout.strip():
        return []
    return ["-c", "user.name=Skillsmith", "-c", "user.email=skillsmith@localhost"]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_S, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"git {args[0]} failed: {e!r}") from e
    if check and done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done
