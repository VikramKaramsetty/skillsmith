"""Iterations strip: one card per attempt (model, change, sim render or annotated key frames),
GET /renders/<run_id>, and tools/render_runs.py rebuilding each version's code from skill.diff."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from test_story_view import NODE, _events, _reducer_js

from skillsmith.dashboard.server import INDEX_HTML, run_renders

REPO = Path(__file__).resolve().parents[2]
V0 = "def run(robot):\n    robot.move_to(0.2, 0.0, 0.05)\n    robot.grasp()\n"
HEAD = "--- skill.py (before)\n+++ skill.py (after)\n"
V0_DIFF = HEAD + "@@ -0,0 +1,3 @@\n" + "".join("+" + ln + "\n" for ln in V0.splitlines())
V2_DIFF = (HEAD + "@@ -1,3 +1,4 @@\n def run(robot):\n-    robot.move_to(0.2, 0.0, 0.05)\n"
           "+    # beside the wall, not over the top\n+    robot.move_to(0.16, 0.0, 0.05)\n     robot.grasp()\n")
V2 = "def run(robot):\n    # beside the wall, not over the top\n    robot.move_to(0.16, 0.0, 0.05)\n    robot.grasp()\n"
STORY = [
    ("run.started", {"task": {"text": "pick up the pink cup", "skill": "pick_cup", "params": {"obj": "pink cup"}}}),
    ("droid.started", {"round": 0, "model": "claude-haiku-5-5"}),
    ("droid.result", {"round": 0, "result": {"change": "first pick: straight down over the cup"}}),
    ("skill.diff", {"old_version": None, "new_version": 0, "diff": V0_DIFF}),
    ("attempt.started", {"env": "sim", "skill": "pick_cup", "version": 0}),
    ("attempt.classified", {"cls": "COLLISION", "bucket": "skill", "summary": "gripper hit pink cup at z=0.095 m"}),
    ("attempt.finished", {"env": "sim", "skill": "pick_cup", "version": 0, "ok": False}),
    ("droid.started", {"round": 1, "model": "claude-haiku-5-5"}),
    ("skill.diff", {"old_version": 0, "new_version": 1, "diff": ""}),
    ("attempt.started", {"env": "sim", "skill": "pick_cup", "version": 1}),
    ("attempt.finished", {"env": "sim", "skill": "pick_cup", "version": 1, "ok": False}),
    ("droid.started", {"round": 2, "model": "claude-opus-5-5"}),
    ("droid.result", {"round": 2, "result": {"change": "approach beside the wall\nmore detail"}}),
    ("skill.diff", {"old_version": 1, "new_version": 2, "diff": V2_DIFF}),
    ("attempt.started", {"env": "sim", "skill": "pick_cup", "version": 2}),
    ("attempt.finished", {"env": "sim", "skill": "pick_cup", "version": 2, "ok": True}),
    ("attempt.started", {"env": "real", "skill": "pick_cup", "version": 2}),
    ("phase.check", {"phase": "approach", "ok": True, "detail": "2 cm off", "frame_path": "runs/r1/frames/a.jpg"}),
    ("phase.check", {"phase": "lift", "ok": False, "detail": "lost it", "frame_path": "runs/r1/frames/l.jpg"}),
    ("phase.check", {"phase": "place", "ok": False, "detail": "skipped", "frame_path": None}),
    ("attempt.classified", {"cls": "SLIP_IN_CARRY", "bucket": "skill", "summary": "lost while lifting"}),
    ("attempt.finished", {"env": "real", "skill": "pick_cup", "version": 2, "ok": False}),
]
RENDERS = {"0": {"video": "runs/r1/sim/v0.mp4", "poster": "runs/r1/sim/v0.jpg", "rerun": {"ok": False}},
           "2": {"video": "runs/r1/sim/v2.mp4", "poster": "runs/r1/sim/v2.jpg", "rerun": {"ok": False}}}


def _iterations(events, renders, tmp_path) -> list[dict]:
    if NODE is None:
        pytest.skip("node not installed")
    lib = tmp_path / "story.js"
    lib.write_text(_reducer_js())
    script = (f"const {{initialState, reduce, iterations}} = require({json.dumps(str(lib))});"
              "const [evs, renders] = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              "process.stdout.write(JSON.stringify(iterations(evs.reduce(reduce, initialState()).drawer, renders)));")
    out = subprocess.run([NODE, "-e", script], input=json.dumps([events, renders]),
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_page_has_the_strip():
    html = INDEX_HTML.read_text()
    for id_ in ["iter-box", "iters", "lightbox"]:
        assert f'id="{id_}"' in html


def test_one_card_per_attempt_with_who_wrote_it(tmp_path):
    cards = _iterations(_events(STORY), {}, tmp_path)
    assert [(c["version"], c["env"], c["ok"]) for c in cards] == [(0, "sim", False), (1, "sim", False),
                                                                   (2, "sim", True), (2, "real", False)]
    assert [c["model"] for c in cards] == ["Haiku", "Haiku", "Opus", "Opus"]
    assert cards[0]["change"] == "first pick: straight down over the cup"
    assert cards[1]["change"] == "same code, run again"
    assert cards[2]["change"] == "approach beside the wall"
    assert cards[0]["cls"] == "COLLISION" and cards[2]["cls"] is None and cards[3]["cls"] == "SLIP_IN_CARRY"


def test_real_attempt_shows_its_annotated_key_frames(tmp_path):
    real = _iterations(_events(STORY), {}, tmp_path)[3]
    assert [(f["phase"], f["ok"]) for f in real["frames"]] == [("approach", True), ("lift", False)]
    assert real["video"] is None


def test_sim_attempts_get_the_rebuilt_render_and_flag_a_changed_outcome(tmp_path):
    cards = _iterations(_events(STORY), RENDERS, tmp_path)
    assert cards[0]["video"] == "runs/r1/sim/v0.mp4" and cards[0]["poster"] == "runs/r1/sim/v0.jpg"
    assert cards[0]["rerun"] is None          # today's sim agrees with the recording
    assert cards[1]["video"] is None          # no render for v1
    assert cards[2]["rerun"] == "FAIL"        # recorded PASS, today's sim FAILs: say so
    assert cards[3]["video"] is None          # real attempts never take a sim render


def test_live_sim_video_wins(tmp_path):
    events = STORY[:5] + [("sim.video", {"path": "runs/r1/live.mp4"})] + STORY[5:7]
    assert _iterations(_events(events), RENDERS, tmp_path)[0]["video"] == "runs/r1/live.mp4"


def _manifest(runs: Path, renders: list[dict]) -> None:
    (runs / "r1" / "sim").mkdir(parents=True)
    (runs / "r1" / "sim" / "manifest.json").write_text(json.dumps({"run_id": "r1", "renders": renders}))


def test_run_renders_keeps_only_files_inside_the_runs_dir(tmp_path):
    runs = tmp_path / "runs"
    _manifest(runs, [{"version": 0, "video": "r1/sim/v0.mp4", "poster": "r1/sim/v0.jpg"},
                     {"version": 1, "video": "r1/sim/gone.mp4", "poster": "r1/sim/v0.jpg"},
                     {"version": 2, "video": "/etc/passwd", "poster": "r1/sim/v0.jpg"}])
    (runs / "r1" / "sim" / "v0.mp4").write_bytes(b"mp4")
    (runs / "r1" / "sim" / "v0.jpg").write_bytes(b"jpg")
    assert [r["version"] for r in run_renders(runs, "r1")["renders"]] == [0]
    assert run_renders(runs, "../r1") is None and run_renders(runs, "nope") is None


def test_renders_endpoint(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from skillsmith.dashboard.server import create_app
    runs = tmp_path / "runs"
    _manifest(runs, [])
    client = TestClient(create_app(runs))
    assert client.get("/renders/r1").json() == {"run_id": "r1", "renders": []}
    assert client.get("/renders/r2").status_code == 404


def _render_runs():
    spec = importlib.util.spec_from_file_location("render_runs", REPO / "tools" / "render_runs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_versions_rebuilt_from_the_recorded_diffs():
    code = _render_runs().versions(_events(STORY))
    assert code[0] == V0 and code[1] == V0
    assert code[2] == V2


def test_sim_attempts_pair_version_scene_and_outcome():
    scene = {"objects": [], "source": "sim"}
    events = _events([("attempt.started", {"env": "sim", "version": 0}),
                      ("scene.observed", {"scene": {"objects": [], "source": "camera:front"}}),
                      ("scene.observed", {"scene": scene}),
                      ("attempt.finished", {"env": "sim", "ok": False, "reason": "COLLISION: hit"}),
                      ("attempt.started", {"env": "real", "version": 0}),
                      ("attempt.finished", {"env": "real", "ok": False})])
    assert list(_render_runs().sim_attempts(events)) == [
        {"version": 0, "scene": scene, "recorded": {"ok": False, "reason": "COLLISION: hit"}}]


def test_a_diff_that_does_not_fit_is_refused():
    with pytest.raises(ValueError):
        _render_runs().apply_diff("something else\n", V2_DIFF)
