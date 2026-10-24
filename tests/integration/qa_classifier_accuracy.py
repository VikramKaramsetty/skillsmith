"""QA.5 helpers: re-classify recorded failures offline and score the classifier against hand labels.

A recorded run is `runs/<run_id>/events.jsonl` (INTERFACES §7). `attempts_from_events` rebuilds each
attempt's `AttemptResult` from the `attempt.started` … `attempt.finished` window (primitive calls,
verify result, error, scenes), so `analysis.analyse_attempt` can be run again on it, with no arm.

Hand labels live next to the recordings in `labels.yaml` (see recorded_failures/README.md):

    - run: 20261024-153000-ab12   # directory holding events.jsonl
      attempt: -1                 # which failed attempt in the run (default: the last one)
      label: SLIP_IN_CARRY        # the true class, a key of types.FAILURE_CLASSES
      note: rim grip, cup slid out on the 16 cm carry

Replay never invents evidence. `safety` and `scene_after` come only from `attempt.finished`; a
`scene.observed` inside the attempt is a mid-skill look(), not the scene after it, and an absent
safety report is not a clean one. A recording that lacks them is *incomplete*: it is listed, never
scored, because the classifier would be judged on evidence it never had.

Scoring follows the rule "a wrong label is worse than UNKNOWN": correct = +1, UNKNOWN = 0,
wrong = -WRONG_PENALTY. Precision per predicted class ignores UNKNOWN (an honest abstention is not
a false positive). A *skill* label on an *environment* failure is counted on its own, because it
sends a table problem to Droid as a code problem, which NORTH_STAR forbids.

Run it by hand on a directory of recordings:

    uv run python tests/integration/qa_classifier_accuracy.py tests/integration/recorded_failures
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import yaml

from skillsmith.types import (
    BUCKET_ENVIRONMENT,
    BUCKET_SKILL,
    FAILURE_CLASSES,
    AttemptResult,
    Classification,
    PrimitiveCall,
    SafetyIssue,
    SafetyReport,
    Scene,
    VerifyResult,
)

UNKNOWN = "UNKNOWN"
WRONG_PENALTY = 2  # one wrong label costs as much as two correct ones earn
NO_LABEL = "<none>"  # analyse_attempt returned no Classification for a failed attempt

Classifier = Callable[[AttemptResult], Optional[Classification]]


# ---- recordings → AttemptResult ---------------------------------------------------------

def read_events(path: Path) -> List[dict]:
    lines = Path(path).read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _scene(d: Optional[dict]) -> Optional[Scene]:
    return Scene.from_dict(d) if d else None


def _vec(v: Optional[list]) -> Optional[tuple]:
    return tuple(v) if v is not None else None


def _call(d: dict) -> PrimitiveCall:
    return PrimitiveCall(index=d["index"], name=d["name"], args=d["args"], t=d["t"],
                         ee_before=_vec(d.get("ee_before")), ee_after=_vec(d.get("ee_after")),
                         ok=d.get("ok", True), result=d.get("result"), error=d.get("error"))


@dataclass
class Replayed:
    """One attempt rebuilt from events. `missing` names evidence the recording lacks; then the
    matching attempt field is None (safety included), never a guess."""
    attempt: AttemptResult
    missing: Tuple[str, ...] = ()


def _safety(d: Optional[dict]) -> Optional[SafetyReport]:
    """None when attempt.finished has no safety report: absent is unknown, not clean."""
    if d is None:
        return None
    return SafetyReport(ok=d["ok"], issues=[SafetyIssue(**i) for i in d.get("issues", [])])


def _missing(finish: dict) -> Tuple[str, ...]:
    """`scene_after: null` is a recorded fact (no scene); a missing key or a null safety is a gap."""
    gaps = [] if finish.get("safety") is not None else ["safety"]
    return tuple(gaps + ([] if "scene_after" in finish else ["scene_after"]))


def _attempt(window: List[dict], scene_before: Optional[Scene]) -> Replayed:
    start, finish = window[0]["data"], window[-1]["data"]
    trace, verify = [], None
    for e in window[1:-1]:
        if e["type"] == "primitive.call":
            trace.append(_call(e["data"]))
        elif e["type"] == "verify.result":
            verify = VerifyResult(ok=e["data"]["ok"], detail=e["data"]["detail"])
    attempt = AttemptResult(
        ok=finish["ok"], env=start["env"], skill=start["skill"], version=start["version"], trace=trace,
        error=finish.get("error"), verify=verify, safety=_safety(finish.get("safety")),
        scene_before=_scene(finish.get("scene_before")) or scene_before,
        scene_after=_scene(finish.get("scene_after")),
        duration_s=finish.get("duration_s", 0.0),
    )
    return Replayed(attempt, _missing(finish))


def attempts_from_events(events: List[dict]) -> List[AttemptResult]:
    """Every complete attempt in a run, in order (see `replay_attempts` for what each one lacks)."""
    return [r.attempt for r in replay_attempts(events)]


def replay_attempts(events: List[dict]) -> List[Replayed]:
    """Every complete attempt in a run, in order. scene_before falls back to the last
    `scene.observed` before the attempt (the loop observes just before running); scene_after and
    safety never fall back to anything."""
    attempts: List[Replayed] = []
    window: Optional[List[dict]] = None
    last_scene: Optional[Scene] = None
    for e in events:
        if e["type"] == "attempt.started":
            window = [e]
            continue
        if window is None:
            if e["type"] == "scene.observed":
                last_scene = _scene(e["data"]["scene"])
            continue
        window.append(e)
        if e["type"] == "attempt.finished":
            attempts.append(_attempt(window, last_scene))
            window = None
    return attempts


# ---- labelled corpus ---------------------------------------------------------------------

@dataclass
class LabelledFailure:
    run: str
    label: str
    attempt: AttemptResult
    note: str = ""
    missing: Tuple[str, ...] = ()  # evidence the recording lacks; such a case is never scored


def load_corpus(root: Path) -> List[LabelledFailure]:
    """Labelled failures under `root` (labels.yaml + <run>/events.jsonl). Empty if no labels yet."""
    labels_path = Path(root) / "labels.yaml"
    if not labels_path.exists():
        return []
    corpus = []
    for entry in yaml.safe_load(labels_path.read_text()) or []:
        label = entry["label"]
        if label not in FAILURE_CLASSES:
            raise ValueError(f"{labels_path}: run {entry['run']}: unknown class {label!r}")
        events = read_events(Path(root) / entry["run"] / "events.jsonl")
        failed = [r for r in replay_attempts(events) if not r.attempt.ok]
        if not failed:
            raise ValueError(f"{labels_path}: run {entry['run']} has no failed attempt")
        case = failed[entry.get("attempt", -1)]
        corpus.append(LabelledFailure(entry["run"], label, case.attempt, entry.get("note", ""), case.missing))
    return corpus


# ---- scoring -----------------------------------------------------------------------------

@dataclass
class ClassScore:
    cls: str
    support: int = 0      # cases whose true label is this class
    predicted: int = 0    # cases the classifier gave this class (UNKNOWN abstentions excluded)
    correct: int = 0
    abstained: int = 0    # true label is this class, classifier said UNKNOWN

    @property
    def precision(self) -> Optional[float]:
        return self.correct / self.predicted if self.predicted else None

    @property
    def recall(self) -> Optional[float]:
        return self.correct / self.support if self.support else None


@dataclass
class AccuracyReport:
    rows: List[tuple] = field(default_factory=list)  # (run, truth, predicted, note)
    incomplete: List[tuple] = field(default_factory=list)  # (run, missing evidence): not scored

    def add(self, run: str, truth: str, predicted: str, note: str = "") -> None:
        self.rows.append((run, truth, predicted, note))

    @property
    def correct(self) -> int:
        return sum(1 for _, t, p, _ in self.rows if p == t)

    @property
    def unknown(self) -> int:
        return sum(1 for _, t, p, _ in self.rows if p == UNKNOWN and t != UNKNOWN)

    @property
    def wrong(self) -> int:
        return len(self.rows) - self.correct - self.unknown

    @property
    def score(self) -> int:
        return self.correct - WRONG_PENALTY * self.wrong

    def env_sent_to_droid(self) -> List[tuple]:
        """Environment failures labelled as a skill class: Droid would get a table problem as code."""
        return [r for r in self.rows
                if FAILURE_CLASSES.get(r[1]) == BUCKET_ENVIRONMENT and FAILURE_CLASSES.get(r[2]) == BUCKET_SKILL]

    def missing(self) -> List[tuple]:
        return [r for r in self.rows if r[2] == NO_LABEL]

    def per_class(self) -> Dict[str, ClassScore]:
        scores: Dict[str, ClassScore] = {}
        for _, truth, pred, _ in self.rows:
            scores.setdefault(truth, ClassScore(truth)).support += 1
            if pred == UNKNOWN and truth != UNKNOWN:
                scores[truth].abstained += 1
                continue
            scores.setdefault(pred, ClassScore(pred)).predicted += 1
            scores[pred].correct += pred == truth
        return dict(sorted(scores.items()))

    def confusion(self) -> Counter:
        return Counter((t, p) for _, t, p, _ in self.rows if t != p)

    def markdown(self) -> str:
        def pct(v: Optional[float]) -> str:
            return "-" if v is None else f"{v:.0%}"

        lines = [f"cases {len(self.rows)} · correct {self.correct} · UNKNOWN {self.unknown} · wrong {self.wrong}"
                 f" · score {self.score} (wrong = -{WRONG_PENALTY})",
                 f"environment failures sent to Droid as skill: {len(self.env_sent_to_droid())}",
                 f"incomplete recordings (not scored): {len(self.incomplete)}"
                 + "".join(f" · {run} lacks {', '.join(gaps)}" for run, gaps in self.incomplete), "",
                 "| class | bucket | support | predicted | correct | UNKNOWN | precision | recall |",
                 "|---|---|---|---|---|---|---|---|"]
        for s in self.per_class().values():
            lines.append(f"| {s.cls} | {FAILURE_CLASSES.get(s.cls, '-')} | {s.support} | {s.predicted} | {s.correct}"
                         f" | {s.abstained} | {pct(s.precision)} | {pct(s.recall)} |")
        if self.confusion():
            lines += ["", "| true | predicted | n |", "|---|---|---|"]
            lines += [f"| {t} | {p} | {n} |" for (t, p), n in self.confusion().most_common()]
        return "\n".join(lines)


def evaluate(corpus: List[LabelledFailure], classify: Classifier) -> AccuracyReport:
    report = AccuracyReport()
    for case in corpus:
        if case.missing:
            report.incomplete.append((case.run, case.missing))
            continue
        c = classify(case.attempt)
        report.add(case.run, case.label, c.cls if c is not None else NO_LABEL, case.note)
    return report


def analysis_classifier(cfg) -> Classifier:
    """The product classifier (WS4.5)."""
    from skillsmith.analysis import analyse_attempt

    return lambda attempt: analyse_attempt(attempt, cfg)[1]


def main(argv: List[str]) -> int:
    from skillsmith.config import load_config

    root = Path(argv[0]) if argv else Path(__file__).parent / "recorded_failures"
    corpus = load_corpus(root)
    if not corpus:
        print(f"no labelled failures under {root} (see recorded_failures/README.md)")
        return 1
    print(evaluate(corpus, analysis_classifier(load_config())).markdown())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
