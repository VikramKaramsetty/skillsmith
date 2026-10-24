"""WS6.6: improvement panel. Python (recorded runs) and the page's reducer (live run) must agree."""

import json
import subprocess

import pytest
from test_story_view import DEMO, FINISH, NODE, _events, _reducer_js, fold

from skillsmith.dashboard.improvement import improvement, run_summary
from skillsmith.dashboard.server import INDEX_HTML

REUSE = [
    ("run.started", {"task": {"text": "stack the blue cup on the red cup", "skill": "stack"}}),
    ("plan.selected", {"skill": "stack", "source": "library", "version": 2}),
    ("attempt.started", {"env": "real", "skill": "stack", "version": 0}),
    ("attempt.finished", {"env": "real", "skill": "stack", "version": 0, "ok": True, "error": None}),
    ("skill.saved", {"skill": "stack", "version": 2}),
    ("run.finished", {"ok": True, "rounds": 0}),
]


def _run(pairs, run_id, t0):
    return [{**e, "run_id": run_id, "t": t0 + e["t"] - 1000.0} for e in _events(pairs)]


def _js(expr: str, args: list, tmp_path):
    if NODE is None:
        pytest.skip("node not installed")
    lib = tmp_path / "story.js"
    lib.write_text(_reducer_js())
    script = (f"const m = require({json.dumps(str(lib))});"
              "const a = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              f"process.stdout.write(JSON.stringify({expr}));")
    out = subprocess.run([NODE, "-e", script], input=json.dumps(args), capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def live_summary(events, tmp_path):
    """The page's live-run summary after these events."""
    state = _js("a.reduce(m.reduceImprove, null)", events, tmp_path)
    return state and state["run"]


def test_improvement_stays_out_of_the_story_state(tmp_path):
    # seconds-to-fix is timing; the story state must not depend on timing (tests/integration QA.3)
    assert "improve" not in fold(_events(DEMO + FINISH), tmp_path)


def test_page_has_the_improvement_panel():
    html = INDEX_HTML.read_text()
    for id_ in ["improve-box", "improve-stats", "improve-runs", "improve-lessons"]:
        assert f'id="{id_}"' in html
    assert 'fetch("/improvement")' in html


def test_repaired_run_summary():
    s = run_summary(_events(DEMO + FINISH))
    assert s["run_id"] == "r1" and s["task"] == "stack the blue cup on the red cup"
    assert (s["ok"], s["rounds"], s["failure"], s["source"], s["reused"]) == (True, 1, "SLIP_IN_CARRY", "library", True)
    assert s["seconds_to_fix"] == 17.0  # attempt.finished (t=1003) → run.finished (t=1020)
    assert s["droid_s"] == 41.0 and s["saved"] == 2 and s["lessons"] == ["cups: grip mid-height"]


def test_reused_run_saves_nothing_new_and_has_no_fix_time():
    s = run_summary(_events(REUSE))
    assert (s["ok"], s["rounds"], s["reused"], s["saved"], s["seconds_to_fix"]) == (True, 0, True, None, None)


def test_running_and_failed_runs():
    assert run_summary(_events(DEMO))["ok"] is None
    failed = run_summary(_events(DEMO + [("run.finished", {"ok": False, "rounds": 2})]))
    assert failed["ok"] is False and failed["rounds"] == 2 and failed["seconds_to_fix"] is None
    assert run_summary(_events([("note", {"text": "no run"})])) is None


def test_page_reducer_matches_python(tmp_path):
    for pairs in [DEMO, DEMO + FINISH, REUSE, DEMO + [("run.finished", {"ok": False, "rounds": 2})]]:
        events = _events(pairs)
        assert live_summary(events, tmp_path) == run_summary(events)


def test_new_run_starts_a_new_summary(tmp_path):
    events = _events(DEMO + FINISH + REUSE)
    s = run_summary(events)
    assert (s["rounds"], s["reused"], s["saved"], s["lessons"]) == (0, True, None, [])
    assert live_summary(events, tmp_path) == s
    assert live_summary(_events([("note", {"text": "before any run"})]), tmp_path) is None


def test_totals_over_runs_show_improvement(tmp_path):
    first, second = run_summary(_run(DEMO + FINISH, "a", 1000.0)), run_summary(_run(REUSE, "b", 2000.0))
    t = _js("m.improvementTotals(a)", [first, second], tmp_path)
    assert t["runs"] == 2 and t["passed"] == 2
    assert (t["repaired"], t["one_round"]) == (1, 1)
    assert t["avg_fix_s"] == 17.0 and t["last_fix_s"] == 17.0
    assert t["skills_saved"] == ["stack"] and t["reused"] == 1
    assert t["lessons"] == ["cups: grip mid-height"] and t["rounds_trend"] == [1, 0]


def test_panel_runs_hide_the_current_run_and_its_future(tmp_path):
    a, b, c = (run_summary(_run(REUSE, rid, t0)) for rid, t0 in [("a", 1000.0), ("b", 2000.0), ("c", 3000.0)])
    ids = _js("m.panelRuns(a[0], a[1]).map((r) => r.run_id)", [[a, b, c], b], tmp_path)
    assert ids == ["a", "b"]  # c started later (a replayed b must not show it); b only once
    assert _js("m.panelRuns(a[0], null).length", [[a, b, c]], tmp_path) == 3


def _record(runs_dir, run_id, events):
    (runs_dir / run_id).mkdir(parents=True)
    (runs_dir / run_id / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))


def test_improvement_reads_recorded_runs_oldest_first(tmp_path):
    runs = tmp_path / "runs"
    _record(runs, "20261024-120500-bbbb", _run(REUSE, "20261024-120500-bbbb", 2000.0))
    _record(runs, "20261024-120000-aaaa", _run(DEMO + FINISH, "20261024-120000-aaaa", 1000.0))
    _record(runs, "empty", [])
    out = improvement(runs)
    assert [s["run_id"] for s in out] == ["20261024-120000-aaaa", "20261024-120500-bbbb"]
    assert [s["rounds"] for s in out] == [1, 0]


def test_improvement_endpoint(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from skillsmith.dashboard.server import create_app
    runs = tmp_path / "runs"
    client = TestClient(create_app(runs))
    assert client.get("/improvement").json() == []
    _record(runs, "r1", _events(DEMO + FINISH))
    (summary,) = client.get("/improvement").json()
    assert summary["seconds_to_fix"] == 17.0 and summary["saved"] == 2


def test_real_demo_fake_log_summary(tmp_path):
    from argparse import Namespace

    from skillsmith.cli import cmd_demo_fake
    from skillsmith.config import Config, load_config

    data = load_config().raw()
    cfg = Config({**data, "repair": {**data["repair"], "runs_dir": str(tmp_path / "runs")}})
    assert cmd_demo_fake(Namespace(author="scripted"), cfg) == 0
    (s,) = improvement(tmp_path / "runs")
    assert s["ok"] is True and s["source"] == "draft" and s["saved"] is not None
    log = next((tmp_path / "runs").glob("*/events.jsonl"))
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert live_summary(events, tmp_path) == s
