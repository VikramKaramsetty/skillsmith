"""WS1.1: robotd's HTTP API (docs/INTERFACES.md §2) over the mock arm and mock cameras."""

import threading
import time

import pytest

from robotd.controller import ArmController
from robotd.mock import MockBus, MockCamera
from robotd.server import create_app

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

OPEN = 100.0


def make_client(bus=None, **kw) -> TestClient:
    ctl = ArmController(bus or MockBus(), max_step_deg=10, max_speed_dps=1000, settle_timeout_s=0.3)
    cams = {"front": MockCamera("front"), "wrist": MockCamera("wrist")}
    return TestClient(create_app(ctl, cams, **kw))


def test_state_returns_the_contract_fields():
    r = make_client().get("/state")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"joints", "holding", "gripper_pos", "gripper_load", "torque", "t"}
    assert len(body["joints"]) == 6


def test_move_joints_blocks_until_the_arm_is_there():
    client = make_client()
    r = client.post("/move_joints", json={"target": [15, 5, -5, 0, 0, OPEN], "speed_dps": 500})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get("/state").json()["joints"][:5] == pytest.approx([15, 5, -5, 0, 0])


def test_bad_target_returns_400_with_a_reason():
    r = make_client().post("/move_joints", json={"target": [1, 2, 3], "speed_dps": 30})
    assert r.status_code == 400
    assert r.json()["ok"] is False and "6" in r.json()["error"]


def test_target_outside_the_joint_limits_returns_400_and_does_not_move():
    client = make_client()
    r = client.post("/move_joints", json={"target": [500, 0, 0, 0, 0, OPEN], "speed_dps": 30})
    assert r.status_code == 400
    assert "shoulder_pan" in r.json()["outside"]
    assert client.get("/state").json()["joints"][0] == 0.0


def test_gripper_reports_holding():
    r = make_client(MockBus(object_at=30.0)).post("/gripper", json={"pct": 0})
    assert r.status_code == 200 and r.json() == {"ok": True, "holding": True}


def test_stop_interrupts_a_blocking_move():
    bus = MockBus()
    client = TestClient(create_app(ArmController(bus, max_step_deg=10, max_speed_dps=100), {}))
    result = {}

    def move():
        result["r"] = client.post("/move_joints", json={"target": [90, 0, 0, 0, 0, OPEN], "speed_dps": 100})

    th = threading.Thread(target=move)
    th.start()
    time.sleep(0.25)
    assert client.post("/stop").json() == {"ok": True}
    th.join(1.0)
    assert not th.is_alive()
    assert result["r"].status_code == 409
    assert 0 < bus.present[0] < 90


def test_torque_off_then_moves_are_refused():
    client = make_client()
    assert client.post("/torque", json={"on": False}).status_code == 200
    assert client.get("/state").json()["torque"] is False
    r = client.post("/move_joints", json={"target": [10, 0, 0, 0, 0, OPEN], "speed_dps": 30})
    assert r.status_code == 409


def test_frame_is_a_jpeg():
    r = make_client().get("/frame/front")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content[:2] == b"\xff\xd8"


def test_unknown_camera_is_404():
    assert make_client().get("/frame/side").status_code == 404


def test_stream_is_mjpeg():
    r = make_client(stream_max_frames=2).get("/stream/wrist")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("multipart/x-mixed-replace")
    assert r.content.count(b"--frame\r\nContent-Type: image/jpeg") == 2
    assert r.content.count(b"\xff\xd8") >= 2


def test_token_is_required_when_set():
    client = make_client(token="s3cret")
    assert client.get("/state").status_code == 401
    assert client.get("/state", headers={"X-Robotd-Token": "wrong"}).status_code == 401
    assert client.get("/state", headers={"X-Robotd-Token": "s3cret"}).status_code == 200
