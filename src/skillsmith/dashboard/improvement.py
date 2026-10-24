"""Improvement panel data (WS6.6): one summary per recorded run, folded from its events only.

The page folds the live run with the same rules (`runSummary` in index.html's story reducer);
tests/ws6/test_improvement.py checks both give the same summary for the same events.

A run summary:
- `rounds`: repair rounds (`run.finished.rounds`, else the `repair.started` count so far).
- `failure`: class of the first `attempt.classified` (what Droid was asked to fix).
- `seconds_to_fix`: first failure (failed attempt, failed gate or rejection) → `run.finished` ok.
  None if the run never failed or never passed. Includes the human's approval time.
- `droid_s`: Droid's own time, the sum of `droid.result.duration`.
- `saved`: the version written to the library (a draft or a repaired skill), else None.
- `reused`: the plan came from the library (`plan.selected.source == "library"`).
- `lessons`: `droid.lesson` texts (appended to skills/lessons.md).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from skillsmith.dashboard.server import list_runs, read_events, run_path


def _failed(e: dict) -> bool:
    d = e.get("data") or {}
    kind = e.get("type")
    return ((kind == "attempt.finished" and not d.get("ok"))
            or (kind == "gate.result" and d.get("ok") is False)
            or (kind == "approval.decided" and d.get("approved") is False))


def run_summary(events: Iterable[dict]) -> Optional[dict]:
    """Summary of one run, or None if the events hold no `run.started`."""
    s: Optional[dict] = None
    failed_at = None
    for e in events:
        d = e.get("data") or {}
        kind = e.get("type")
        if kind == "run.started":
            task = d.get("task") or {}
            s = {"run_id": e.get("run_id"), "t": e.get("t"), "task": task.get("text"), "skill": task.get("skill"),
                 "source": None, "ok": None, "rounds": 0, "failure": None, "seconds_to_fix": None,
                 "droid_s": 0.0, "saved": None, "reused": False, "lessons": []}
            failed_at = None
            continue
        if s is None:
            continue
        if failed_at is None and _failed(e):
            failed_at = e.get("t")
        if kind == "plan.selected":
            s.update(skill=d.get("skill") or s["skill"], source=d.get("source"), reused=d.get("source") == "library")
        elif kind == "repair.started":
            s["rounds"] = max(s["rounds"], int(d.get("round") or 0))
        elif kind == "attempt.classified" and s["failure"] is None:
            s["failure"] = d.get("cls")
        elif kind == "droid.result":
            s["droid_s"] += float(d.get("duration") or 0.0)
        elif kind == "droid.lesson" and d.get("lesson"):
            s["lessons"].append(d["lesson"])
        elif kind == "skill.saved":
            if s["source"] == "draft" or s["rounds"] > 0:  # a reused skill re-emits its old version
                s["saved"] = d.get("version")
        elif kind == "run.finished":
            s.update(ok=bool(d.get("ok")), rounds=int(d.get("rounds", s["rounds"])))
            if s["ok"] and failed_at is not None and e.get("t") is not None:
                s["seconds_to_fix"] = e["t"] - failed_at
    return s


def improvement(runs_dir: Path) -> list[dict]:
    """Summaries of every recorded run under runs_dir, oldest first."""
    out = []
    for run in list_runs(runs_dir):
        path = run_path(runs_dir, run["run_id"])
        summary = run_summary(read_events(path)) if path else None
        if summary is not None:
            out.append(summary)
    return sorted(out, key=lambda s: (s["t"] or 0.0, s["run_id"] or ""))
