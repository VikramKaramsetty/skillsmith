"""WS6.5: live camera (robotd passthrough), world-state map from events, sim replay video."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_story_view import DEMO, NODE, _events, _reducer_js, fold

from skillsmith.dashboard.server import INDEX_HTML, VIDEO_TYPES, frame_path

BLUE = {"id": "blue_cup_0", "label": "blue cup", "color": "blue", "kind": "cup", "x": 0.22, "y": -0.08,
        "z": 0.0, "height": 0.08, "width": 0.065, "confidence": 1.0, "visible": True}
RED = {**BLUE, "id": "red_cup_0", "label": "red cup", "color": "red", "y": 0.08}
BLOCK = {**BLUE, "id": "green_block_0", "label": "green block", "color": "green", "kind": "block",
         "x": 0.15, "y": 0.0, "width": 0.03, "confidence": 0.3, "visible": False}
SCENE = ("scene.observed", {"scene": {"objects": [BLUE, RED, BLOCK], "t": 999.0, "source": "camera:front",
                                      "image_path": "runs/r1/scene.jpg"}, "image_path": "runs/r1/scene.jpg"})


def call(i, name, ee, ok=True, result=None):
    return ("primitive.call", {"index": i, "name": name, "args": {}, "t": 1.0, "ee_before": None,
                               "ee_after": ee, "ok": ok, "result": result, "error": None})


PICK = [SCENE, call(0, "move_to", [0.22, -0.08, 0.07]), call(1, "grasp", [0.22, -0.08, 0.07], result=True),
        call(2, "move_to", [0.22, 0.08, 0.18])]


def _js(fn: str, events, tmp_path):
    if NODE is None:
        pytest.skip("node not installed")
    lib = tmp_path / "story.js"
    lib.write_text(_reducer_js())
    script = (f"const m = require({json.dumps(str(lib))});"
              "const evs = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              f"process.stdout.write(JSON.stringify(m.{fn}(evs.reduce(m.reduce, m.initialState()).world)));")
    out = subprocess.run([NODE, "-e", script], input=json.dumps(events), capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_page_has_the_world_panel():
    html = INDEX_HTML.read_text()
    for id_ in ["world-box", "camera", "map", "world-text", "sim-video", "cams"]:
        assert f'id="{id_}"' in html
    assert "/camera/" in html and "/video?path=" in html


def test_scene_fills_the_world(tmp_path):
    w = fold(_events(DEMO[:1] + [SCENE]), tmp_path)["world"]
    assert [o["id"] for o in w["objects"]] == ["blue_cup_0", "red_cup_0", "green_block_0"]
    assert w["source"] == "camera:front" and w["image_path"] == "runs/r1/scene.jpg"


def test_grasp_holds_the_nearest_object_and_it_follows_the_gripper(tmp_path):
    w = fold(_events(DEMO[:1] + PICK), tmp_path)["world"]
    assert w["held"] == "blue_cup_0" and w["ee"] == [0.22, 0.08, 0.18]
    m = _js("tableMap", _events(DEMO[:1] + PICK), tmp_path)
    blue = next(o for o in m["objects"] if o["id"] == "blue_cup_0")
    assert blue["held"] and (blue["cx"], blue["cy"]) == (m["ee"]["cx"], m["ee"]["cy"])
    assert (m["ee"]["cx"], m["ee"]["cy"]) == (170, 180)  # 1 px = 1 mm; y left = left, x forward = up
    block = next(o for o in m["objects"] if o["id"] == "green_block_0")
    assert block["uncertain"] and not block["visible"]
    line = _js("worldLine", _events(DEMO[:1] + PICK), tmp_path)
    assert line == "3 object(s) from camera:front · holding blue cup · gripper at (22, 8, 18) cm"


def test_failed_grasp_holds_nothing(tmp_path):
    events = [SCENE, call(0, "grasp", [0.22, -0.08, 0.07], result=False)]
    assert fold(_events(DEMO[:1] + events), tmp_path)["world"]["held"] is None


def test_release_leaves_the_object_estimated_until_the_next_scene(tmp_path):
    placed = PICK + [call(3, "release", [0.22, 0.08, 0.15])]
    w = fold(_events(DEMO[:1] + placed), tmp_path)["world"]
    blue = w["objects"][0]
    assert w["held"] is None and blue["estimated"] and (blue["x"], blue["y"]) == (0.22, 0.08)
    w = fold(_events(DEMO[:1] + placed + [SCENE]), tmp_path)["world"]
    assert "estimated" not in w["objects"][0] and w["objects"][0]["y"] == -0.08


def test_bump_shows_until_the_next_scene(tmp_path):
    bump = ("world.bumped", {"message": "red cup moved 3.2 cm while not held"})
    s = fold(_events(DEMO[:1] + PICK + [bump]), tmp_path)
    assert s["world"]["bumped"] == "red cup moved 3.2 cm while not held"
    assert s["banner"]["text"].startswith("STOPPED")
    assert fold(_events(DEMO[:1] + PICK + [bump, SCENE]), tmp_path)["world"]["bumped"] is None


def test_sim_video_until_the_next_version(tmp_path):
    video = ("sim.video", {"path": "runs/r1/sim_v2.mp4"})
    assert fold(_events(DEMO + [video]), tmp_path)["world"]["video"] == "runs/r1/sim_v2.mp4"
    diff = ("skill.diff", {"old_version": 2, "new_version": 3, "diff": ""})
    assert fold(_events(DEMO + [video, diff]), tmp_path)["world"]["video"] is None


def test_new_run_clears_the_world(tmp_path):
    w = fold(_events(DEMO[:1] + PICK + DEMO[:1]), tmp_path)["world"]
    assert w["objects"] == [] and w["ee"] is None and w["held"] is None


def test_real_demo_fake_log_fills_the_world(tmp_path):
    from argparse import Namespace

    from skillsmith.cli import cmd_demo_fake
    from skillsmith.config import Config, load_config

    data = load_config().raw()
    cfg = Config({**data, "repair": {**data["repair"], "runs_dir": str(tmp_path / "runs")}})
    assert cmd_demo_fake(Namespace(author="scripted"), cfg) == 0
    (log,) = (tmp_path / "runs").glob("*/events.jsonl")
    w = fold([json.loads(line) for line in log.read_text().splitlines()], tmp_path)["world"]
    assert w["objects"] and w["ee"] is not None and w["held"] is None


# ---- GET /video and GET /camera/{cam} -------------------------------------------------------

def test_video_path_stays_inside_runs_dir(tmp_path):
    runs = tmp_path / "runs"
    (runs / "r1").mkdir(parents=True)
    mp4 = runs / "r1" / "sim.mp4"
    mp4.write_bytes(b"mp4")
    (runs / "r1" / "kf.jpg").write_bytes(b"jpg")
    assert frame_path(runs, "r1/sim.mp4", VIDEO_TYPES) == mp4.resolve()
    assert frame_path(runs, "r1/kf.jpg", VIDEO_TYPES) is None
    assert frame_path(runs, "../sim.mp4", VIDEO_TYPES) is None


def _client(tmp_path, **kw):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from skillsmith.dashboard.server import create_app
    return TestClient(create_app(tmp_path / "runs", **kw))


def test_video_endpoint(tmp_path):
    (tmp_path / "runs" / "r1").mkdir(parents=True)
    (tmp_path / "runs" / "r1" / "sim.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    client = _client(tmp_path)
    r = client.get("/video", params={"path": "r1/sim.mp4"})
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert client.get("/video", params={"path": "/etc/passwd"}).status_code == 404


MJPEG = b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\n\xff\xd8\xff\xd9\r\n" * 3


class _FakeRobotd(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path != "/stream/front":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        self.wfile.write(MJPEG)

    def log_message(self, *args):
        pass


@pytest.fixture
def robotd():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeRobotd)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_camera_passes_robotd_stream_through(tmp_path, robotd):
    r = _client(tmp_path, camera_url=robotd).get("/camera/front")
    assert r.status_code == 200
    assert r.headers["content-type"] == "multipart/x-mixed-replace; boundary=frame"
    assert r.content == MJPEG


def test_camera_errors(tmp_path, robotd):
    assert _client(tmp_path, camera_url=robotd).get("/camera/kitchen").status_code == 404
    assert _client(tmp_path).get("/camera/front").status_code == 503  # no robotd configured
    r = _client(tmp_path, camera_url="http://127.0.0.1:9").get("/camera/front")  # nothing listens
    assert r.status_code == 503 and "offline" in r.json()["detail"]
