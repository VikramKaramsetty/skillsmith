"""WS5.6 seeded-bug benchmark: fix rate, one-round fix rate and seconds-to-fix per failure class,
with and without evidence v2.

Each case is the stack skill with one seeded bug, run through the real RepairLoop on the fake
world (rim slip on, like `make demo-fake`; the sim check env is a fake world without it). The
bug is the author's draft, so it goes through the gates like Droid's own code. The author then
repairs it with evidence v2 (the product) or v1 (the pre-v2 report: raw error, trace, scenes).

- **fixed:** the run ends with the camera check passing within `max_rounds` repairs.
- **one-round fix:** fixed with exactly one repair.
- **seconds-to-fix:** first `repair.started` → `run.finished ok=True`, from event timestamps.

Labels come from `skillsmith.analysis.analyse_attempt`. Until WS4.5 lands it raises
NotImplementedError and the case's seeded class stands in (`labels: seeded` in the results),
so the benchmark measures the repair, not the classifier.

    python -m skillsmith.repair.benchmark --author droid            # real Droid via the SDK
    python -m skillsmith.repair.benchmark --author oracle           # plumbing check, no Droid
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import statistics
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence

from skillsmith.config import Config, load_config
from skillsmith.events import EventBus, new_run_id
from skillsmith.hal.base import Env, make_env
from skillsmith.hal.fake import FakeWorld
from skillsmith.repair.approvers import AutoApprover
from skillsmith.repair.loop import EVIDENCE_VERSIONS, RepairLoop
from skillsmith.repair.protocols import AuthorContext, SkillAuthor
from skillsmith.repair.report import unlabelled_reason
from skillsmith.skills.library import SkillLibrary
from skillsmith.tasks import parse_task
from skillsmith.types import FAILURE_CLASSES, AttemptResult, Classification, TaskSpec

TASK = "stack the blue cup on the red cup"

_STACK = '''"""stack: put one object on top of another."""

SKILL = {{"name": "stack", "params": {{"top": "str", "bottom": "str"}}, "uses": []}}

TRAVEL_Z = {travel_z}


def run(robot, scene, skills, top, bottom):
    {lookup}
    b = scene.require(bottom)
    robot.note(f"stacking {{a.label}} on {{b.label}}")
    grip_z = {grip_z}
    robot.release()
    robot.move_to(a.x, a.y, TRAVEL_Z)
    robot.move_to(a.x, a.y, grip_z)
    {grasp}
    robot.move_to(a.x, a.y, TRAVEL_Z)
    robot.move_to({place_xy}, TRAVEL_Z)
    if not robot.holding():
        raise SkillError(f"lost {{a.label}} while carrying it")
    robot.move_to({place_xy}, {place_z})
    robot.release()
    robot.move_to({place_xy}, TRAVEL_Z)
    robot.home()
'''

_CORRECT = {
    "travel_z": "0.18",
    "lookup": "a = scene.require(top)",
    "grip_z": "a.z + a.height * 0.5  # mid-height",
    "grasp": 'if not robot.grasp():\n        raise SkillError(f"missed {a.label}")',
    "place_xy": "b.x, b.y",
    "place_z": "b.top + (grip_z - a.z) + 0.005",
}


def stack_source(**bug: str) -> str:
    """The stack skill with the given slots replaced by a bug (no slots: the correct skill)."""
    unknown = set(bug) - set(_CORRECT)
    if unknown:
        raise KeyError(f"unknown stack slots: {sorted(unknown)}")
    return _STACK.format(**{**_CORRECT, **bug})


@dataclass(frozen=True)
class SeededBug:
    name: str
    cls: str     # the class a correct classifier gives it (a key of FAILURE_CLASSES)
    phase: str
    hint: str
    bug: Dict[str, str]
    task: str = TASK

    @property
    def source(self) -> str:
        return stack_source(**self.bug)

    @property
    def fix(self) -> str:
        """A known-good repair (the oracle author's answer)."""
        return stack_source()


CASES: List[SeededBug] = [
    SeededBug("rim_grip", "SLIP_IN_CARRY", "carry", "grip height",
              {"grip_z": "a.top - 0.01  # grab near the rim"}),
    SeededBug("grip_above_top", "GRASP_MISS", "grasp", "grip height",
              {"grip_z": "a.top + 0.02"}),
    SeededBug("never_grasps", "NO_PICK", "grasp", "grasp call",
              {"grasp": "robot.note(\"gripper ready\")"}),
    SeededBug("low_travel", "COLLISION", "carry", "travel height",
              {"travel_z": "0.09"}),
    SeededBug("place_ignores_height", "COLLISION", "place", "place z",
              {"place_z": "b.z + (grip_z - a.z)"}),
    SeededBug("place_wrong_axis", "PLACE_OFF_TARGET", "place", "place x, y",
              {"place_xy": "b.x, a.y"}),
    SeededBug("travel_above_workspace", "UNSAFE_TARGET", "approach", "travel height",
              {"travel_z": "0.30"}),
    SeededBug("label_typo", "SKILL_ERROR", "approach", "object lookup",
              {"lookup": "a = scene.require(top.label)"}),
]


@dataclass
class CaseResult:
    case: str
    cls: str
    evidence: str
    repeat: int
    fixed: bool
    rounds: int
    seconds: Optional[float]   # seconds-to-fix; None when not fixed
    seeded_failed: bool        # the seed broke the first run (else the case is invalid)
    labels: str                # "classifier" | "seeded"
    observed_cls: Optional[str]  # the first failure's label
    error: Optional[str] = None  # the author or loop raised


@dataclass
class ClassSummary:
    cls: str
    evidence: str
    n: int
    fix_rate: float
    one_round_rate: float
    median_seconds: Optional[float]


@dataclass
class BenchmarkResult:
    author: str
    max_rounds: int
    cases: List[CaseResult] = field(default_factory=list)

    def summary(self) -> List[ClassSummary]:
        out = []
        for ev in dict.fromkeys(c.evidence for c in self.cases):
            for cls in dict.fromkeys(c.cls for c in self.cases):
                rows = [c for c in self.cases if c.evidence == ev and c.cls == cls]
                if rows:
                    out.append(_summarise(cls, ev, rows))
            out.append(_summarise("all", ev, [c for c in self.cases if c.evidence == ev]))
        return out

    def table(self) -> str:
        """Markdown: one row per class, v1 and v2 side by side."""
        evs = list(dict.fromkeys(c.evidence for c in self.cases))
        by = {(s.cls, s.evidence): s for s in self.summary()}
        head = ["class", "n"] + [f"{m} ({ev})" for m in ("fix rate", "one-round", "median s") for ev in evs]
        rows = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        for cls in dict.fromkeys(s.cls for s in self.summary()):
            first = next(by[(cls, ev)] for ev in evs if (cls, ev) in by)
            cells = [f"**{cls}**" if cls == "all" else f"`{cls}`", str(first.n)]
            for metric in ("fix_rate", "one_round_rate", "median_seconds"):
                for ev in evs:
                    s = by.get((cls, ev))
                    cells.append("—" if s is None else _fmt(metric, getattr(s, metric)))
            rows.append("| " + " | ".join(cells) + " |")
        labels = sorted({c.labels for c in self.cases})
        notes = [f"author: `{self.author}` · max_rounds {self.max_rounds} · labels: {', '.join(labels)}"]
        invalid = sorted({c.case for c in self.cases if not c.seeded_failed})
        if invalid:
            notes.append(f"**seed did not fail (invalid):** {', '.join(invalid)}")
        return "\n".join(rows) + "\n\n" + "\n".join(notes) + "\n"

    def to_json(self) -> dict:
        return {"author": self.author, "max_rounds": self.max_rounds,
                "cases": [asdict(c) for c in self.cases], "summary": [asdict(s) for s in self.summary()]}


def _summarise(cls: str, evidence: str, rows: List[CaseResult]) -> ClassSummary:
    secs = [c.seconds for c in rows if c.fixed and c.seconds is not None]
    return ClassSummary(cls, evidence, len(rows),
                        sum(c.fixed for c in rows) / len(rows),
                        sum(c.fixed and c.rounds == 1 for c in rows) / len(rows),
                        round(statistics.median(secs), 2) if secs else None)


def _fmt(metric: str, value) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}" if metric == "median_seconds" else f"{value:.0%}"


# ---- authors --------------------------------------------------------------------------------
AuthorFactory = Callable[[Config, EventBus, Callable, SeededBug], SkillAuthor]


class SeededAuthor:
    """Drafts the seeded bug, then hands every repair to the author under test."""

    def __init__(self, inner: SkillAuthor, bug_source: str):
        self.inner = inner
        self.bug_source = bug_source

    @property
    def last_result(self) -> Optional[dict]:  # DroidSDKAuthor's change_type feeds escalation
        return getattr(self.inner, "last_result", None)

    def draft(self, task: TaskSpec, ctx: AuthorContext) -> str:
        return self.bug_source

    def repair(self, task: TaskSpec, source: str, report_md: str, ctx: AuthorContext) -> str:
        return self.inner.repair(task, source, report_md, ctx)


class OracleAuthor:
    """Answers every repair with the case's known fix: checks the harness, not Droid."""

    def __init__(self, fix: str):
        self.fix = fix
        self.reports: List[str] = []

    def draft(self, task: TaskSpec, ctx: AuthorContext) -> str:
        raise RuntimeError("OracleAuthor only repairs")

    def repair(self, task: TaskSpec, source: str, report_md: str, ctx: AuthorContext) -> str:
        self.reports.append(report_md)
        return self.fix


def oracle_author(cfg, bus, make_check_env, case: SeededBug) -> SkillAuthor:
    return OracleAuthor(case.fix)


def droid_author(cfg, bus, make_check_env, case: SeededBug) -> SkillAuthor:
    from skillsmith.repair.droid_sdk import DroidSDKAuthor

    return DroidSDKAuthor(cfg, bus, make_check_env)


AUTHORS: Dict[str, AuthorFactory] = {"oracle": oracle_author, "droid": droid_author}


# ---- labels ---------------------------------------------------------------------------------
@contextlib.contextmanager
def _labels(case: SeededBug, used: List[str]) -> Iterator[None]:
    """Use the real classifier; while it is a stub, label failures with the seeded class."""
    import skillsmith.analysis as analysis

    real = analysis.analyse_attempt

    def label(attempt: AttemptResult, cfg):
        try:
            out = real(attempt, cfg)
            used.append("classifier")
            return out
        except NotImplementedError:
            used.append("seeded")
            return [], (None if attempt.ok else seeded_label(case, attempt))

    analysis.analyse_attempt = label
    try:
        yield
    finally:
        analysis.analyse_attempt = real


def seeded_label(case: SeededBug, attempt: AttemptResult) -> Classification:
    failed = [c.index for c in attempt.trace if not c.ok]
    call = failed[0] if failed else (attempt.trace[-1].index if attempt.trace else None)
    return Classification(case.cls, FAILURE_CLASSES[case.cls], case.phase, call,
                          unlabelled_reason(attempt), hint=case.hint)


# ---- running --------------------------------------------------------------------------------
def run_case(case: SeededBug, evidence: str, author_factory: AuthorFactory, cfg: Config, *,
             repeat: int = 0, runs_dir: Optional[Path] = None) -> CaseResult:
    """One seeded bug through the real RepairLoop on the fake world, in its own workspace and
    lessons file so cases never learn from each other."""
    cfg = _isolated(cfg)
    bus = EventBus(f"bench-{case.name}-{evidence}-{repeat}-{new_run_id()}", runs_dir)
    events: List[dict] = []
    bus.subscribe(events.append)
    world = FakeWorld.demo(cfg, rim_slip=True)

    def make_check_env(scene):
        return make_env("fake", cfg, scene)

    author = author_factory(cfg, bus, make_check_env, case)
    loop = RepairLoop(cfg, Env("fake", world, world, world), make_check_env,
                      SkillLibrary(_temp_library()), SeededAuthor(author, case.source),
                      AutoApprover("fake"), bus, evidence=evidence)
    used: List[str] = []
    error, ok, rounds = None, False, 0
    with _labels(case, used):
        try:
            result = loop.run(parse_task(case.task))
            ok, rounds = result.ok, result.rounds
        except Exception as e:  # Droid timeout, droid CLI missing, ...: the case failed, the bench goes on
            error = repr(e)
            bus.emit("error", where="benchmark.case", case=case.name, message=error)
            rounds = sum(e["type"] == "repair.started" for e in events)
        finally:
            close = getattr(author, "close", None)
            if callable(close):
                close()
            bus.close()
    classified = [e["data"]["cls"] for e in events if e["type"] == "attempt.classified"]
    return CaseResult(case.name, case.cls, evidence, repeat, ok, rounds, _seconds_to_fix(events) if ok else None,
                      seeded_failed=_seed_failed(events), labels=used[0] if used else "seeded",
                      observed_cls=classified[0] if classified else None, error=error)


def run_benchmark(author: str = "oracle", *, cfg: Optional[Config] = None,
                  cases: Optional[Sequence[SeededBug]] = None, evidence: Sequence[str] = EVIDENCE_VERSIONS,
                  repeats: int = 1, max_rounds: Optional[int] = None, runs_dir: Optional[Path] = None,
                  author_factory: Optional[AuthorFactory] = None) -> BenchmarkResult:
    cfg = cfg or load_config()
    if max_rounds is not None:
        cfg = _with(cfg, repair={"max_rounds": max_rounds})
    factory = author_factory or AUTHORS[author]
    out = BenchmarkResult(author, int(cfg.repair.max_rounds))
    for ev in evidence:
        for case in cases or CASES:
            for r in range(repeats):
                out.cases.append(run_case(case, ev, factory, cfg, repeat=r, runs_dir=runs_dir))
    return out


def _seconds_to_fix(events: List[dict]) -> Optional[float]:
    start = next((e["t"] for e in events if e["type"] == "repair.started"), None)
    end = next((e["t"] for e in events if e["type"] == "run.finished" and e["data"].get("ok")), None)
    return None if start is None or end is None else round(end - start, 3)


def _seed_failed(events: List[dict]) -> bool:
    """The seed broke something (a gate or the target attempt), so the author was asked to repair."""
    return any(e["type"] == "repair.started" for e in events)


def _isolated(cfg: Config) -> Config:
    root = Path(tempfile.mkdtemp(prefix="skillsmith-bench-"))
    return _with(cfg, repair={"droid_ws_root": str(root / "droid_ws"), "lessons_file": str(root / "lessons.md")})


def _with(cfg: Config, **sections: dict) -> Config:
    data = copy.deepcopy(cfg.raw())
    for name, values in sections.items():
        data.setdefault(name, {}).update(values)
    return Config(data)


def _temp_library() -> Path:
    from skillsmith.demo import temp_library

    return temp_library()


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m skillsmith.repair.benchmark", description=__doc__.splitlines()[0])
    p.add_argument("--author", choices=sorted(AUTHORS), default="oracle")
    p.add_argument("--evidence", nargs="+", choices=EVIDENCE_VERSIONS, default=list(EVIDENCE_VERSIONS))
    p.add_argument("--cases", nargs="+", choices=[c.name for c in CASES], help="default: all")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--max-rounds", type=int, default=None, help="default: repair.max_rounds")
    p.add_argument("--out", default=None, help="write the full results as JSON here")
    p.add_argument("--runs-dir", default=None, help="default: <repair.runs_dir>/benchmark-<id>")
    p.add_argument("--config", default=None)
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    runs_dir = Path(args.runs_dir) if args.runs_dir else cfg.path(cfg.repair.runs_dir) / f"benchmark-{new_run_id()}"
    cases = [c for c in CASES if args.cases is None or c.name in args.cases]
    result = run_benchmark(args.author, cfg=cfg, cases=cases, evidence=args.evidence,
                           repeats=args.repeats, max_rounds=args.max_rounds, runs_dir=runs_dir)
    print(result.table())
    for c in result.cases:
        if c.error:
            print(f"- {c.case} ({c.evidence}): {c.error}", file=sys.stderr)
    out = Path(args.out) if args.out else runs_dir / "results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.to_json(), indent=2))
    print(f"results: {out}  events: {runs_dir}/<case run>/events.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
