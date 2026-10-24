"""WS6.4: detail drawer. Key frames, phase table, Droid tool calls and permission decisions."""

import json
import re
import subprocess

import pytest
from test_story_view import DEMO, FINISH, NODE, _events, _reducer_js, fold

from skillsmith.dashboard.server import INDEX_HTML, frame_path

PHASES = [
    ("phase.check", {"phase": "approach", "ok": True, "detail": "over blue cup, 0.4 cm off", "start_index": 0,
                     "end_index": 2, "frame_path": None}),
    ("phase.check", {"phase": "grasp", "ok": True, "detail": "holding", "start_index": 3, "end_index": 4,
                     "frame_path": "runs/r1/kf_grasp.jpg"}),
    ("phase.check", {"phase": "carry", "ok": False, "detail": "blue cup 4.2 cm below gripper", "start_index": 5,
                     "end_index": 8, "frame_path": "runs/r1/kf_carry.jpg"}),
]
DROID = [
    ("droid.tools", {"tools": ["skillsmith___get_scene", "skillsmith___simulate_skill", "Edit"]}),
    ("droid.tool_call", {"round": 1, "name": "skillsmith___simulate_skill", "input": "{}"}),
    ("droid.check", {"round": 1, "tool": "simulate_skill", "ok": True, "run": 1, "reason": "ok"}),
    ("droid.tool_result", {"round": 1, "tool": "skillsmith___simulate_skill", "is_error": False,
                           "content": "PASS: the task succeeded in simulation."}),
    ("droid.permission", {"round": 1, "action": "EditAction", "target": "skill.py", "allowed": True,
                          "reason": "edit of skill.py in the workspace"}),
]
# DEMO: run.started, plan, attempt.started, attempt.finished, classified, repair, droid.started, message, permission...
STORY = DEMO[:3] + PHASES + DEMO[3:7] + DROID + DEMO[7:] + FINISH


def _keyframes(events, tmp_path) -> list[dict]:
    if NODE is None:
        pytest.skip("node not installed")
    lib = tmp_path / "story.js"
    lib.write_text(_reducer_js())
    script = (f"const {{initialState, reduce, keyframes}} = require({json.dumps(str(lib))});"
              "const evs = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              "process.stdout.write(JSON.stringify(keyframes(evs.reduce(reduce, initialState()).drawer)));")
    out = subprocess.run([NODE, "-e", script], input=json.dumps(events), capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_page_has_the_drawer():
    html = INDEX_HTML.read_text()
    for id_ in ["drawer", "drawer-toggle", "frames", "phases", "permissions", "steps", "session"]:
        assert f'id="{id_}"' in html


def test_page_scripts_parse(tmp_path):
    """The DOM half isn't run in tests, so at least make sure node can parse every script."""
    if NODE is None:
        pytest.skip("node not installed")
    page = tmp_path / "page.js"
    page.write_text("\n".join(re.findall(r"^<script[^>]*>(.*?)</script>", INDEX_HTML.read_text(), re.S | re.M)))
    out = subprocess.run([NODE, "--check", str(page)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr


def test_phase_table_per_attempt_with_classification(tmp_path):
    d = fold(_events(STORY), tmp_path)["drawer"]
    first, second = d["attempts"]
    assert (first["version"], first["ok"], second["version"], second["ok"]) == (1, False, 2, True)
    assert [p["phase"] for p in first["phases"]] == ["approach", "grasp", "carry"]
    assert first["phases"][2]["ok"] is False
    assert first["classification"]["cls"] == "SLIP_IN_CARRY"
    assert second["phases"] == []


def test_keyframes_come_from_phase_checks(tmp_path):
    frames = _keyframes(_events(STORY), tmp_path)
    assert [(f["phase"], f["ok"], f["path"], f["attempt"]) for f in frames] == [
        ("grasp", True, "runs/r1/kf_grasp.jpg", 1), ("carry", False, "runs/r1/kf_carry.jpg", 1)]


def test_tool_calls_checks_and_permissions_in_order(tmp_path):
    d = fold(_events(STORY), tmp_path)["drawer"]
    assert [(s["kind"], s["name"], s["ok"]) for s in d["steps"]] == [
        ("call", "skillsmith___simulate_skill", None), ("check", "simulate_skill", True),
        ("result", "skillsmith___simulate_skill", True)]
    assert d["steps"][1]["text"] == "run 1: ok"
    perms = [(p["action"], p["allowed"]) for p in d["permissions"]]
    assert perms == [("EditAction", True), ("shell", False)]
    refused = d["permissions"][1]
    assert refused["reason"] == "no shell" and refused["round"] == 1


def test_session_shows_the_controls(tmp_path):
    s = fold(_events(STORY), tmp_path)["drawer"]["session"]
    assert s["autonomy"] == "low" and s["disabled_tools"] == ["shell"] and s["via"] == "droid-sdk"
    assert "skillsmith___simulate_skill" in s["tools"]


def test_long_tool_output_is_clipped(tmp_path):
    big = ("droid.tool_result", {"round": 1, "tool": "Read", "is_error": True, "content": "x" * 5000})
    (step,) = fold(_events(DEMO[:1] + [big]), tmp_path)["drawer"]["steps"]
    assert step["ok"] is False and len(step["text"]) == 400


def test_new_run_clears_the_drawer(tmp_path):
    d = fold(_events(STORY + DEMO[:1]), tmp_path)["drawer"]
    assert d == {"attempts": [], "session": None, "steps": [], "permissions": []}


def test_phase_check_without_attempt_started_is_kept(tmp_path):
    d = fold(_events(PHASES[:1]), tmp_path)["drawer"]
    assert len(d["attempts"]) == 1 and d["attempts"][0]["phases"][0]["phase"] == "approach"


def test_real_demo_fake_log_fills_the_drawer(tmp_path):
    from argparse import Namespace

    from skillsmith.cli import cmd_demo_fake
    from skillsmith.config import Config, load_config

    data = load_config().raw()
    cfg = Config({**data, "repair": {**data["repair"], "runs_dir": str(tmp_path / "runs")}})
    assert cmd_demo_fake(Namespace(author="scripted"), cfg) == 0
    (log,) = (tmp_path / "runs").glob("*/events.jsonl")
    d = fold([json.loads(line) for line in log.read_text().splitlines()], tmp_path)["drawer"]
    assert d["attempts"] and d["attempts"][-1]["ok"] is True


# ---- GET /frame ---------------------------------------------------------------------------

def test_frame_path_stays_inside_runs_dir(tmp_path):
    runs = tmp_path / "runs"
    (runs / "r1").mkdir(parents=True)
    img = runs / "r1" / "kf_carry.jpg"
    img.write_bytes(b"\xff\xd8jpeg")
    (tmp_path / "secret.png").write_bytes(b"png")
    (runs / "r1" / "events.jsonl").write_text("{}\n")
    assert frame_path(runs, str(img)) == img.resolve()
    assert frame_path(runs, "r1/kf_carry.jpg") == img.resolve()
    assert frame_path(runs, str(tmp_path / "secret.png")) is None
    assert frame_path(runs, "../secret.png") is None
    assert frame_path(runs, "r1/events.jsonl") is None  # images only
    assert frame_path(runs, "r1/missing.jpg") is None


def test_frame_endpoint_serves_images_only_from_runs(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from skillsmith.dashboard.server import create_app

    runs = tmp_path / "runs"
    (runs / "r1").mkdir(parents=True)
    (runs / "r1" / "kf.png").write_bytes(b"\x89PNG data")
    client = TestClient(create_app(runs))
    r = client.get("/frame", params={"path": str(runs / "r1" / "kf.png")})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == b"\x89PNG data"
    assert client.get("/frame", params={"path": "/etc/passwd"}).status_code == 404
