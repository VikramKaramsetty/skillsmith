"""WS5.6: seeded-bug benchmark (fix rate, one-round fix rate, seconds-to-fix per class, v1 vs v2).

Real Droid numbers need `python -m skillsmith.repair.benchmark --author droid`; here the oracle
author checks the harness, a class-keyed author checks that v1 and v2 really differ, and the
fake droid_sdk runs one case through DroidSDKAuthor.
"""

import json

import fake_droid_sdk as fake
import pytest

from skillsmith.config import load_config
from skillsmith.demo import temp_library
from skillsmith.events import NullBus
from skillsmith.hal.base import Env, make_env
from skillsmith.hal.fake import FakeWorld
from skillsmith.repair import benchmark as bench
from skillsmith.repair import droid_sdk as author_mod
from skillsmith.repair.approvers import AutoApprover
from skillsmith.repair.benchmark import CASES, OracleAuthor, run_benchmark, run_case, stack_source
from skillsmith.repair.droid_tools import EVIDENCE_TOOLS
from skillsmith.repair.loop import RepairLoop
from skillsmith.skills.library import SkillLibrary
from skillsmith.skills.runtime import run_skill
from skillsmith.skills.validator import validate
from skillsmith.tasks import parse_task

cfg = load_config()
TASK = parse_task(bench.TASK)
BY_NAME = {c.name: c for c in CASES}


def _run(source, rim_slip):
    w = FakeWorld.demo(cfg, rim_slip=rim_slip)
    return run_skill(source, Env("fake", w, w, w), TASK.params, cfg, NullBus(), skill="stack",
                     success=TASK.success)


class Recording:
    """An author factory that keeps every author it builds."""

    def __init__(self, make):
        self.make = make
        self.authors = []

    def __call__(self, cfg, bus, make_check_env, case, *, evidence="v2"):
        author = self.make(case)
        self.authors.append(author)
        return author


class ClassKeyedAuthor(OracleAuthor):
    """Fixes only when the report names the seeded class: stands in for 'evidence that says what
    went wrong helps', so the benchmark must show v2 > v1 with it."""

    def __init__(self, case):
        super().__init__(case.fix)
        self.case = case

    def repair(self, task, source, report_md, ctx):
        self.reports.append(report_md)
        return self.fix if f"`{self.case.cls}`" in report_md else source.replace("TRAVEL_Z =", "TRAVEL_Z = 0.01 +")


def test_correct_skill_passes_and_every_seed_breaks_it():
    assert not validate(stack_source())
    assert _run(stack_source(), rim_slip=True).ok
    for case in CASES:
        assert not validate(case.source), case.name  # bugs are runtime bugs, not static ones
        sim, real = _run(case.source, rim_slip=False), _run(case.source, rim_slip=True)
        assert not real.ok, case.name
        assert sim.ok == (case.name == "rim_grip"), case.name  # only rim slip is a sim-to-real gap


def test_oracle_benchmark_fixes_every_class_in_one_round_with_both_evidence_versions():
    result = run_benchmark("oracle", cfg=cfg)
    assert len(result.cases) == 2 * len(CASES)
    assert all(c.seeded_failed and c.fixed and c.rounds == 1 and c.error is None for c in result.cases)
    assert all(c.observed_cls == c.cls and c.labels == "seeded" for c in result.cases)
    assert all(c.seconds is not None and c.seconds >= 0 for c in result.cases)
    overall = {s.evidence: s for s in result.summary() if s.cls == "all"}
    assert overall["v1"].fix_rate == overall["v2"].one_round_rate == 1.0

    table = result.table()
    assert "fix rate (v1)" in table and "one-round (v2)" in table and "median s (v2)" in table
    for cls in {c.cls for c in CASES}:
        assert f"`{cls}`" in table
    assert "| `COLLISION` | 2 |" in table and "invalid" not in table
    assert bench.SEEDED_CAVEAT in table  # seeded labels are an oracle class: say so next to the numbers
    json.dumps(result.to_json())


def test_v1_evidence_has_no_class_phases_or_tools_and_v2_does():
    case = BY_NAME["rim_grip"]
    seen = {}
    for ev in ("v1", "v2"):
        factory = Recording(lambda c: CtxSpy(c.fix))
        assert run_case(case, ev, factory, cfg).fixed
        (author,) = factory.authors
        seen[ev] = (author.reports[0], author.ctxs[0])
    v1, v1_ctx = seen["v1"]
    v2, v2_ctx = seen["v2"]
    assert "SLIP_IN_CARRY" not in v1 and "## Phases" not in v1 and "Failure class" not in v1
    assert "lost blue cup" in v1 and "## Trace" in v1
    assert v1_ctx.attempt is None and v1_ctx.expected is None  # no evidence tools in v1
    assert v2.splitlines()[2].startswith("**Failure class:** `SLIP_IN_CARRY`")
    assert v2_ctx.attempt is not None and v2_ctx.expected is not None  # sim run that passed


class CtxSpy(OracleAuthor):
    def __init__(self, fix):
        super().__init__(fix)
        self.ctxs = []

    def repair(self, task, source, report_md, ctx):
        self.ctxs.append(ctx)
        return super().repair(task, source, report_md, ctx)


def test_benchmark_separates_v1_from_v2():
    result = run_benchmark(cfg=cfg, max_rounds=2, author_factory=Recording(ClassKeyedAuthor))
    overall = {s.evidence: s for s in result.summary() if s.cls == "all"}
    assert overall["v2"].fix_rate == overall["v2"].one_round_rate == 1.0
    assert overall["v1"].fix_rate == 0.0 and overall["v1"].median_seconds is None
    assert all(c.rounds == 2 for c in result.cases if c.evidence == "v1")
    assert "| **all** | 8 | 0% | 100% |" in result.table()


def test_author_crash_is_a_failed_case_not_a_crashed_benchmark():
    class Crashes(OracleAuthor):
        def repair(self, *a):
            raise TimeoutError("droid took too long")

    result = run_benchmark(cfg=cfg, cases=CASES[:2], evidence=["v2"],
                           author_factory=Recording(lambda c: Crashes(c.fix)))
    assert [c.fixed for c in result.cases] == [False, False]
    assert all("droid took too long" in c.error and c.rounds == 1 for c in result.cases)
    assert next(s for s in result.summary() if s.cls == "all").fix_rate == 0.0


def test_real_classifier_is_used_once_it_exists(monkeypatch):
    from skillsmith.types import Classification

    def classifier(attempt, _cfg):
        return [], None if attempt.ok else Classification("UNKNOWN", "unknown", None, None, "no idea")

    monkeypatch.setattr("skillsmith.analysis.analyse_attempt", classifier)
    res = run_case(BY_NAME["grip_above_top"], "v2", Recording(lambda c: OracleAuthor(c.fix)), cfg)
    assert res.labels == "classifier" and res.observed_cls == "UNKNOWN" and res.fixed
    assert bench.SEEDED_CAVEAT not in bench.BenchmarkResult("oracle", 1, [res]).table()
    import skillsmith.analysis as analysis
    assert analysis.analyse_attempt is classifier  # the benchmark put it back


def test_droid_sdk_author_runs_a_case(monkeypatch, tmp_path):
    droid = fake.install(monkeypatch)
    monkeypatch.setattr(author_mod.shutil, "which", lambda name: f"/opt/bin/{name}")
    case = BY_NAME["place_wrong_axis"]
    droid.turns = [fake.Turn(skill=case.fix, output=fake.output(change_type="parameter"))]
    res = run_case(case, "v2", bench.droid_author, cfg, runs_dir=tmp_path)
    assert res.fixed and res.rounds == 1 and res.error is None
    (session,) = droid.sessions
    assert session.closed and "PLACE_OFF_TARGET" in session.prompts[0][0]
    (run_dir,) = tmp_path.iterdir()
    assert (run_dir / "events.jsonl").exists()


def test_cli_prints_the_table_and_writes_json(tmp_path, capsys):
    out = tmp_path / "results.json"
    assert bench.main(["--author", "oracle", "--cases", "rim_grip", "label_typo", "--out", str(out),
                       "--runs-dir", str(tmp_path / "runs")]) == 0
    printed = capsys.readouterr().out
    assert "`SLIP_IN_CARRY`" in printed and "`SKILL_ERROR`" in printed
    data = json.loads(out.read_text())
    assert data["author"] == "oracle" and len(data["cases"]) == 4


def test_unknown_evidence_version_is_refused():
    w = FakeWorld.demo(cfg)
    with pytest.raises(ValueError, match="evidence"):
        RepairLoop(cfg, Env("fake", w, w, w), lambda s: make_env("fake", cfg, s), SkillLibrary(temp_library()),
                   OracleAuthor(""), AutoApprover("fake"), NullBus(), evidence="v3")


# ---- #157: no slot-revealing hints; v1 gets the v1 rules and no evidence tools -------------------
SLOT_WORDS = ["grip height", "grip z", "grip_z", "travel height", "travel z", "travel_z", "place x", "place y",
              "place_xy", "place z", "place_z", "place height", "grasp call", "lookup", "require", "label"]


def test_seeded_hints_never_name_a_template_slot():
    assert not hasattr(CASES[0], "hint")  # the old per-case hint named the bugged slot
    assert set(bench._CORRECT) <= {w.replace(" ", "_") for w in SLOT_WORDS} | {"grasp", "lookup"}
    for hint in bench.CLASS_HINTS.values():
        assert not [w for w in SLOT_WORDS if w in hint.lower()], hint
    for case in CASES:
        factory = Recording(lambda c: OracleAuthor(c.fix))
        assert run_case(case, "v2", factory, cfg).fixed
        report = factory.authors[0].reports[0]
        (look,) = [line for line in report.splitlines() if line.startswith("**Look at:**")]
        assert look == f"**Look at:** {bench.CLASS_HINTS[case.cls]}", case.name
        assert not [w for w in SLOT_WORDS if w in look.lower()], (case.name, look)


V2_MARKERS = ["Failure class", "## Phases", "phase table", "expected vs", "key frames", "get_frame",
              "get_object_track", "compare_with_success", "value-only change", "frames/"]


def test_v1_droid_gets_the_v1_rules_and_no_evidence_tools(monkeypatch, tmp_path):
    droid = fake.install(monkeypatch)
    monkeypatch.setattr(author_mod.shutil, "which", lambda name: f"/opt/bin/{name}")
    case = BY_NAME["rim_grip"]
    droid.turns = [fake.Turn(skill=case.fix, output=fake.output(change_type="parameter"))]
    res = run_case(case, "v1", bench.droid_author, cfg, runs_dir=tmp_path)
    assert res.fixed and res.error is None
    (session,) = droid.sessions
    prompt = session.prompts[0][0]
    append = session.config.system_prompt["append"]
    (server,) = session.config.mcp_servers
    tools = [t.name for t in server.tools]
    for marker in V2_MARKERS:
        assert marker not in prompt, marker
        assert marker not in append, marker
    assert "SLIP_IN_CARRY" not in prompt and "## Trace" in prompt
    assert "# AGENTS.md" in append and "# API.md" in append and "trace table" in append
    assert not set(tools) & set(EVIDENCE_TOOLS) and "simulate_skill" in tools
    assert not [t for t in session.config.restrict_tools if t.split("___")[-1] in EVIDENCE_TOOLS]


def test_v2_droid_keeps_the_v2_rules_and_evidence_tools(monkeypatch, tmp_path):
    droid = fake.install(monkeypatch)
    monkeypatch.setattr(author_mod.shutil, "which", lambda name: f"/opt/bin/{name}")
    case = BY_NAME["rim_grip"]
    droid.turns = [fake.Turn(skill=case.fix, output=fake.output(change_type="parameter"))]
    assert run_case(case, "v2", bench.droid_author, cfg, runs_dir=tmp_path).fixed
    (session,) = droid.sessions
    append = session.config.system_prompt["append"]
    assert append.startswith(author_mod.SYSTEM_APPEND) and "compare_with_success(phase)" in append
    (server,) = session.config.mcp_servers
    assert set(EVIDENCE_TOOLS) <= {t.name for t in server.tools}


def test_v1_rules_differ_from_v2_only_in_the_evidence_steps():
    from skillsmith.repair.workspace import TEMPLATE_DIR

    v1, v2 = ((TEMPLATE_DIR / n).read_text() for n in ("AGENTS_v1.md", "AGENTS.md"))
    tail = "## No magic offsets"
    assert v1[v1.index(tail):] == v2[v2.index(tail):]  # everything below the job steps stays in sync
    assert author_mod.EVIDENCE_TOOLS_HINT in author_mod.SYSTEM_APPEND
    assert author_mod.EVIDENCE_TOOLS_HINT not in author_mod.system_text("v1")
    with pytest.raises(ValueError, match="evidence"):
        author_mod.DroidSDKAuthor(cfg, NullBus(), lambda s: None, evidence="v3")
