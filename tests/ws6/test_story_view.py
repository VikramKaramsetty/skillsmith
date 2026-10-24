"""WS6.2: big-screen story view. The page's pure reducer is run under node over recorded events."""

import json
import re
import shutil
import subprocess

import pytest

from skillsmith.dashboard.server import INDEX_HTML

NODE = shutil.which("node")
DIFF = """--- skill.py (before)
+++ skill.py (after)
@@ -10,7 +10,7 @@
-    robot.move_to(x, y, top_z - 0.01)
+    robot.move_to(x, y, mid_z)
     robot.grasp()
-    robot.move_to(x, y, TRAVEL_Z)
+    robot.move_to(x, y, TRAVEL_Z, speed=0.5)
+    a
+    b
+    c
"""

# The G5 demo story in INTERFACES §7 event shapes.
DEMO = [
    ("run.started", {"task": {"text": "stack the blue cup on the red cup", "skill": "stack"}}),
    ("plan.selected", {"skill": "stack", "source": "library", "version": 1}),
    ("attempt.started", {"env": "real", "skill": "stack", "version": 1}),
    ("attempt.finished", {"env": "real", "skill": "stack", "version": 1, "ok": False,
                          "error": "SkillError: lost blue cup", "reason": "SLIP_IN_CARRY: cup slipped"}),
    ("attempt.classified", {"cls": "SLIP_IN_CARRY", "bucket": "skill", "phase": "carry", "call_index": 8,
                            "summary": "blue cup dropped 4.2 cm below the gripper during carry"}),
    ("repair.started", {"round": 1, "reason": "# Failure report"}),
    ("droid.started", {"round": 1, "via": "droid-sdk", "workspace": "/tmp/ws", "autonomy": "low",
                       "disabled_tools": ["shell"]}),
    ("droid.message", {"round": 1, "text": "The rim grip slips on a cup.\nI will grip lower."}),
    ("droid.permission", {"round": 1, "action": "shell", "target": "ls", "allowed": False, "reason": "no shell"}),
    ("droid.result", {"round": 1, "subtype": "success", "session_id": "s", "duration": 41, "credits": 1,
                      "result": {"diagnosis": "Grip at the rim slips on a tapered cup; grip at mid-height.",
                                 "change": "grasp z from rim to mid-height", "sim_checked": True,
                                 "confidence": 0.8, "lesson": "cups: grip mid-height"}}),
    ("droid.lesson", {"round": 1, "lesson": "cups: grip mid-height"}),
    ("skill.diff", {"old_version": 1, "new_version": 2, "diff": DIFF}),
    ("gate.result", {"gate": "static", "ok": True, "detail": "ok"}),
    ("gate.result", {"gate": "sim", "ok": True, "detail": "task achieved in sim"}),
    ("gate.result", {"gate": "safety", "ok": True, "detail": "all safety checks passed"}),
    ("approval.requested", {"summary": {"title": "run stack v2 on real", "skill": "stack", "version": 2}}),
]
FINISH = [
    ("approval.decided", {"approved": True, "note": ""}),
    ("attempt.started", {"env": "real", "skill": "stack", "version": 2}),
    ("attempt.finished", {"env": "real", "skill": "stack", "version": 2, "ok": True, "error": None}),
    ("skill.saved", {"skill": "stack", "version": 2}),
    ("run.finished", {"ok": True, "rounds": 1}),
]


def _events(pairs):
    return [{"t": 1000.0 + i, "run_id": "r1", "type": t, "data": d} for i, (t, d) in enumerate(pairs)]


def _reducer_js() -> str:
    m = re.search(r'^<script id="story-reducer">(.*?)</script>', INDEX_HTML.read_text(), re.S | re.M)
    assert m, "index.html must keep the pure reducer in <script id=\"story-reducer\">"
    return m.group(1)


def fold(events: list[dict], tmp_path) -> dict:
    """Final story state after feeding events through the page's reducer."""
    if NODE is None:
        pytest.skip("node not installed")
    lib = tmp_path / "story.js"
    lib.write_text(_reducer_js())
    script = (f"const {{initialState, reduce}} = require({json.dumps(str(lib))});"
              "const evs = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              "process.stdout.write(JSON.stringify(evs.reduce(reduce, initialState())));")
    out = subprocess.run([NODE, "-e", script], input=json.dumps(events), capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_page_has_every_big_screen_element():
    html = INDEX_HTML.read_text()
    for id_ in ["task", "banner", "droid", "diff", "gates", "approve"]:
        assert f'id="{id_}"' in html


def test_fail_banner_shows_class_and_call(tmp_path):
    s = fold(_events(DEMO[:5]), tmp_path)
    assert s["task"] == "stack the blue cup on the red cup"
    assert s["banner"]["kind"] == "fail"
    assert s["banner"]["text"] == "FAIL · SLIP_IN_CARRY at call #8"
    assert "4.2 cm" in s["banner"]["sub"]


def test_classification_before_finish_is_kept(tmp_path):
    pairs = DEMO[:3] + [DEMO[4], DEMO[3]]
    s = fold(_events(pairs), tmp_path)
    assert s["banner"]["text"] == "FAIL · SLIP_IN_CARRY at call #8"


def test_environment_bucket_says_human(tmp_path):
    env = ("attempt.classified", {"cls": "CAMERA_BUMPED", "bucket": "environment", "phase": None,
                                  "call_index": None, "summary": "marker drift 2.1 cm"})
    s = fold(_events(DEMO[:4] + [env]), tmp_path)
    assert s["banner"]["text"] == "FAIL · CAMERA_BUMPED"
    assert "human" in s["banner"]["sub"]


def test_unclassified_failure_falls_back_to_reason(tmp_path):
    s = fold(_events(DEMO[:4]), tmp_path)
    assert s["banner"] == {"kind": "fail", "text": "FAIL", "sub": "SLIP_IN_CARRY: cup slipped"}


def test_droid_diagnosis_short_diff_and_gates(tmp_path):
    s = fold(_events(DEMO), tmp_path)
    assert s["droid"]["diagnosis"].startswith("Grip at the rim slips")
    assert s["droid"]["via"] == "droid-sdk"
    assert s["diff"]["new_version"] == 2
    assert [line["kind"] for line in s["diff"]["lines"]] == ["del", "add", "del", "add", "add"]
    assert s["diff"]["more"] == 2
    assert {g: v["state"] for g, v in s["gates"].items()} == {"static": "ok", "sim": "ok", "safety": "ok"}
    assert s["approval"]["state"] == "pending"
    assert s["banner"]["kind"] == "wait"


def test_droid_message_shows_until_result(tmp_path):
    s = fold(_events(DEMO[:8]), tmp_path)
    assert s["droid"]["diagnosis"] is None
    assert s["droid"]["message"] == "The rim grip slips on a cup."
    assert s["banner"]["text"] == "DROID REPAIRING · round 1"


def test_pass_banner_after_approval(tmp_path):
    s = fold(_events(DEMO + FINISH), tmp_path)
    assert s["approval"]["state"] == "approved"
    assert s["banner"]["kind"] == "pass"
    assert s["banner"]["text"] == "PASS · stack v2 saved · lesson learned"


def test_failed_gate_and_new_diff_resets_lights(tmp_path):
    bad = ("gate.result", {"gate": "sim", "ok": False, "detail": "blue cup fell in sim"})
    s = fold(_events(DEMO[:13] + [bad]), tmp_path)
    assert s["gates"]["sim"]["state"] == "fail"
    assert s["banner"]["text"] == "GATE FAILED · sim"
    s = fold(_events(DEMO[:13] + [bad, DEMO[11]]), tmp_path)
    assert all(g["state"] == "pending" for g in s["gates"].values())


def test_rejection_and_failed_run(tmp_path):
    pairs = DEMO + [("approval.decided", {"approved": False, "note": "too fast"}),
                    ("run.finished", {"ok": False, "rounds": 2})]
    s = fold(_events(pairs), tmp_path)
    assert s["approval"] == {"state": "rejected", "title": "run stack v2 on real", "note": "too fast"}
    assert s["banner"]["text"] == "FAILED · after 2 round(s)"


def test_error_event_shows_on_banner(tmp_path):
    s = fold(_events([("error", {"where": "dashboard.replay", "message": "bad JSON"})]), tmp_path)
    assert s["banner"]["kind"] == "error" and "bad JSON" in s["banner"]["sub"]


def test_real_demo_fake_log_ends_in_pass(tmp_path):
    from argparse import Namespace

    from skillsmith.cli import cmd_demo_fake
    from skillsmith.config import Config, load_config

    data = load_config().raw()
    cfg = Config({**data, "repair": {**data["repair"], "runs_dir": str(tmp_path / "runs")}})
    assert cmd_demo_fake(Namespace(author="scripted"), cfg) == 0
    (log,) = (tmp_path / "runs").glob("*/events.jsonl")
    s = fold([json.loads(line) for line in log.read_text().splitlines()], tmp_path)
    assert s["task"] == "stack the blue cup on the red cup"
    assert s["banner"]["kind"] == "pass" and s["banner"]["text"].startswith("PASS · stack v1 saved")
    assert s["diff"]["old_version"] == 0 and s["diff"]["new_version"] == 1
    assert {g["state"] for g in s["gates"].values()} == {"ok"}
    assert s["approval"]["state"] == "approved"


def test_new_run_resets_story(tmp_path):
    s = fold(_events(DEMO + FINISH + DEMO[:1]), tmp_path)
    assert s["diff"] is None and s["saved"] is None and s["banner"]["text"] == "STARTING"
