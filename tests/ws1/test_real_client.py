"""WS1.5: RobotdArm (hal/real.py) against robotd's own app over the mock arm, and make_real_env wiring."""

import copy
import http.server
import threading
import time
from contextlib import contextmanager

import pytest

from robotd.controller import ArmController
from robotd.mock import MockBus, MockCamera
from skillsmith.config import Config, load_config
from skillsmith.events import NullBus
from skillsmith.hal import real
from skillsmith.hal.real import RobotdArm, RobotdError, make_real_env

OPEN = 100.0


@contextmanager
def robotd(bus=None, token=None):
    """robotd's FastAPI app under uvicorn on a free loopback port (mock arm, mock cameras)."""
    pytest.importorskip("fastapi")
    uvicorn = pytest.importorskip("uvicorn")
    from robotd.server import create_app

    ctl = ArmController(bus or MockBus(), max_step_deg=10, max_speed_dps=1000, settle_timeout_s=0.3)
    app = create_app(ctl, {"front": MockCamera("front"), "wrist": MockCamera("wrist")}, token=token)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), "robotd (mock) did not start"
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@contextmanager
def slow_robotd(move_s: float):
    """A stdlib stand-in whose /move_joints hangs for move_s; records every path it was asked for."""
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/move_joints":
                time.sleep(move_s)
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", hits
    finally:
        server.shutdown()
        server.server_close()


def free_url() -> str:
    """A loopback URL nobody listens on."""
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    port = server.server_address[1]
    server.server_close()
    return f"http://127.0.0.1:{port}"


def cfg_at(url: str) -> Config:
    data = copy.deepcopy(load_config().raw())
    data["robot"]["robotd_url"] = url
    return Config(data)


# ---- RobotdArm ------------------------------------------------------------------------------

def test_arm_reads_six_joints_and_moves():
    with robotd() as url:
        arm = RobotdArm(url)
        assert arm.name == "real"
        assert arm.joints() == pytest.approx([0, 0, 0, 0, 0, OPEN])
        arm.move_joints([15, -10, 20, 5, 0, OPEN], 500)
        assert arm.joints()[:5] == pytest.approx([15, -10, 20, 5, 0], abs=1.5)


def test_gripper_closes_and_reports_holding():
    with robotd(MockBus(object_at=30.0)) as url:
        arm = RobotdArm(url)
        assert arm.gripper_holding() is False
        arm.set_gripper(0)
        assert arm.gripper_holding() is True
        arm.set_gripper(OPEN)
        assert arm.gripper_holding() is False


def test_refusal_raises_and_emits_an_error_event_without_moving():
    bus = NullBus()
    with robotd() as url:
        arm = RobotdArm(url, bus=bus)
        with pytest.raises(RobotdError) as info:
            arm.move_joints([500, 0, 0, 0, 0, OPEN], 30)
        assert arm.joints()[0] == 0.0
    err = info.value
    assert err.status == 400 and "shoulder_pan" in err.details["outside"]
    assert "shoulder_pan" in str(err)
    events = [e for e in bus.events if e["type"] == "error"]
    assert len(events) == 1
    assert events[0]["data"] == {"where": "robotd.move_joints", "message": str(err)}


def test_stop_answers_and_the_arm_still_reads():
    with robotd() as url:
        arm = RobotdArm(url)
        arm.stop()
        assert len(arm.joints()) == 6


def test_token_is_sent_when_robotd_requires_one():
    with robotd(token="s3cret") as url:
        with pytest.raises(RobotdError) as info:
            RobotdArm(url).joints()
        assert info.value.status == 401
        assert len(RobotdArm(url, token="s3cret").joints()) == 6


def test_unreachable_robotd_says_how_to_start_it():
    bus = NullBus()
    with pytest.raises(RobotdError) as info:
        RobotdArm(free_url(), timeout_s=1.0, bus=bus).joints()
    assert info.value.status == 0
    assert "python -m robotd" in str(info.value)
    assert [e["data"]["where"] for e in bus.events] == ["robotd.state"]


def test_a_move_that_times_out_sends_stop_before_raising():
    bus = NullBus()
    with slow_robotd(move_s=1.0) as (url, hits):
        arm = RobotdArm(url, move_timeout_s=0.2, bus=bus)
        with pytest.raises(RobotdError) as info:
            arm.move_joints([10, 0, 0, 0, 0, OPEN], 30)
        assert info.value.status == 0
        assert hits == ["/move_joints", "/stop"]
    assert "robotd.move_joints" in [e["data"]["where"] for e in bus.events]


def test_error_payloads_without_json_still_read():
    class Raw:
        code, reason = 503, "Service Unavailable"

        def read(self):
            return b"<html>bad gateway</html>"

    err = real._refusal("GET /state", Raw())
    assert err.status == 503 and "Service Unavailable" in str(err) and err.details == {}


# ---- make_real_env ----------------------------------------------------------------------------

class FakeKin:
    def fk(self, joints):
        return (0.2, 0.0, 0.1)

    def ik(self, target, seed):
        return list(seed)


class RecordingWorld:
    """Stands in for WS3's WorldModel; records what make_real_env handed it."""

    def __init__(self, cfg, frame_source=None, kinematics=None, arm=None, bus=None):
        self.frame_source, self.kinematics, self.arm, self.bus = frame_source, kinematics, arm, bus
        self.started = self.closed = False

    def start(self):
        self.started = True
        return self

    def close(self):
        self.closed = True


class Closable:
    closed = 0

    def close(self):
        Closable.closed += 1


def test_make_real_env_wires_robotd_placo_and_the_world_model(monkeypatch):
    import skillsmith.kinematics
    import skillsmith.perception.world
    from skillsmith.hal.base import make_env
    from skillsmith.perception.frames import RobotdFrames

    kin = FakeKin()
    monkeypatch.setattr(skillsmith.kinematics, "make_kinematics", lambda cfg: kin)
    monkeypatch.setattr(skillsmith.perception.world, "WorldModel", RecordingWorld)
    monkeypatch.setenv("ROBOTD_TOKEN", "s3cret")
    with robotd(token="s3cret") as url:
        env = make_env("real", cfg_at(url))
        try:
            assert env.name == "real" and isinstance(env.arm, RobotdArm) and env.kin is kin
            assert env.arm.url == url and env.arm.token == "s3cret"
            world = env.perception
            assert world.arm is env.arm and world.kinematics is kin
            assert world.started  # the live loop runs from the start
            assert isinstance(world.frame_source, RobotdFrames)
            assert world.frame_source.url == f"{url}/frame/front"
            assert world.frame_source.read().jpeg  # the world model sees robotd's front camera
            env.arm.move_joints([5, 0, 0, 0, 0, OPEN], 500)
            assert env.arm.joints()[0] == pytest.approx(5, abs=1.5)
        finally:
            env.close()
        assert world.closed


class NoDetections:
    def detect(self, image):
        return []


def test_make_real_env_runs_the_real_world_model_loop_over_robotd(monkeypatch):
    """WS3's actual WorldModel: its loop reads robotd's front camera and the arm's FK tip."""
    import skillsmith.kinematics
    import skillsmith.perception.frames
    import skillsmith.perception.world

    kin = FakeKin()
    monkeypatch.setattr(skillsmith.kinematics, "make_kinematics", lambda cfg: kin)
    monkeypatch.setattr(skillsmith.perception.world, "_default_camera", lambda cfg: object())
    monkeypatch.setattr(skillsmith.perception.world, "_default_detector", lambda cfg: NoDetections())
    monkeypatch.setattr(skillsmith.perception.frames.Frame, "bgr", lambda self: self.jpeg)  # no opencv
    bus = NullBus()
    with robotd() as url:
        env = make_real_env(cfg_at(url), bus=bus)
        try:
            world = env.perception
            assert isinstance(world, skillsmith.perception.world.WorldModel)
            assert world.arm is env.arm and world.kin is kin and world.bus is bus
            deadline = time.monotonic() + 5
            while world.rate() == 0.0:
                assert time.monotonic() < deadline, f"world model loop never stepped: {bus.types()}"
                time.sleep(0.02)
            scene = env.perception.observe()
            assert scene.source == "camera:front" and scene.objects == ()
            assert "error" not in bus.types()
        finally:
            env.close()
        assert world._thread is None  # Env.close stopped the loop


def test_make_real_env_without_camera_calibration_says_how_to_make_it(monkeypatch, tmp_path):
    import skillsmith.kinematics
    import skillsmith.perception.frames
    from skillsmith.perception.geometry import CalibrationMissing

    Closable.closed = 0
    monkeypatch.setattr(skillsmith.kinematics, "make_kinematics", lambda cfg: FakeKin())
    monkeypatch.setattr(skillsmith.perception.frames, "make_frame_source", lambda cfg, cam: Closable())
    with robotd() as url:
        cfg = cfg_at(url)
        cfg.raw()["cameras"]["calibration"] = str(tmp_path / "missing.json")
        with pytest.raises(CalibrationMissing, match="calibrate_camera"):
            make_real_env(cfg)
    assert Closable.closed == 1


def test_make_real_env_fails_fast_when_robotd_is_down(monkeypatch):
    import skillsmith.kinematics

    built = []
    monkeypatch.setattr(skillsmith.kinematics, "make_kinematics", lambda cfg: built.append(cfg))
    bus = NullBus()
    with pytest.raises(RobotdError, match="python -m robotd"):
        make_real_env(cfg_at(free_url()), bus=bus)
    assert built == []  # nothing else is built against a dead robotd
    assert bus.types() == ["error"]


def test_make_real_env_closes_the_frame_source_when_the_world_model_fails(monkeypatch):
    import skillsmith.kinematics
    import skillsmith.perception.frames
    import skillsmith.perception.world

    def not_ready(*args, **kw):
        raise NotImplementedError("WS3.5")

    Closable.closed = 0
    monkeypatch.setattr(skillsmith.kinematics, "make_kinematics", lambda cfg: FakeKin())
    monkeypatch.setattr(skillsmith.perception.frames, "make_frame_source", lambda cfg, cam: Closable())
    monkeypatch.setattr(skillsmith.perception.world, "WorldModel", not_ready)
    with robotd() as url:
        with pytest.raises(NotImplementedError):
            make_real_env(cfg_at(url))
    assert Closable.closed == 1
