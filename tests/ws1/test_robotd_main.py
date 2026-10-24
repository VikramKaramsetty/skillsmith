"""WS1.1/WS1.2: `python -m robotd` argument handling (uvicorn is replaced by a recorder, the arm by a mock)."""

import pytest
import yaml
from conftest import goals

from robotd import feetech
from robotd.__main__ import DEFAULT_CONFIG, build_controller, main
from robotd.mock import MockBus


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, app, host, port):
        self.calls.append((app, host, port))


@pytest.fixture(autouse=True)
def no_token_env(monkeypatch):
    monkeypatch.delenv("ROBOTD_TOKEN", raising=False)


class ClosingBus(MockBus):
    closed = False

    def close(self):
        self.closed = True


def test_without_mock_it_opens_the_follower_port_named_in_the_config(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    robot = yaml.safe_load(DEFAULT_CONFIG.read_text())["robot"]
    opened = []
    monkeypatch.setattr(feetech, "connect_follower", lambda port, cid: opened.append((port, cid)) or ClosingBus())
    serve = Recorder()
    assert main([], serve=serve) == 0
    assert opened == [(robot["follower_port"], robot["calibration_id"])]
    [(app, _, _)] = serve.calls
    assert TestClient(app).get("/frame/front").status_code == 503  # a real (here: fake, silent) camera, never mock frames


def test_real_cameras_come_from_the_config(monkeypatch, never_open_real_cameras):
    cams = yaml.safe_load(DEFAULT_CONFIG.read_text())["cameras"]
    monkeypatch.setattr(feetech, "connect_follower", lambda port, cid: ClosingBus())
    assert main([], serve=Recorder()) == 0
    assert sorted(never_open_real_cameras.opened, key=str) == sorted(
        [(cams["front"], None), (cams["wrist"], tuple(cams["wrist_size"]))], key=str)


def test_cameras_only_never_opens_the_arm(monkeypatch, never_open_real_cameras):
    opened = []
    monkeypatch.setattr(feetech, "connect_follower", lambda port, cid: opened.append(port) or ClosingBus())
    assert main(["--cameras-only"], serve=Recorder()) == 0
    assert opened == []
    assert len(never_open_real_cameras.opened) == 2


def test_when_the_arm_cannot_be_opened_it_exits_1_with_the_reason(monkeypatch, capsys):
    def fail(port, cid):
        raise RuntimeError("calibration mismatch on shoulder_lift")

    monkeypatch.setattr(feetech, "connect_follower", fail)
    serve = Recorder()
    assert main([], serve=serve) == 1
    assert serve.calls == []
    assert "calibration mismatch on shoulder_lift" in capsys.readouterr().err


def test_the_bus_is_closed_when_the_server_stops(monkeypatch):
    bus = ClosingBus()
    monkeypatch.setattr(feetech, "connect_follower", lambda port, cid: bus)
    assert main([], serve=Recorder()) == 0
    assert bus.closed is True


def test_a_refused_host_never_opens_the_arm(monkeypatch):
    opened = []
    monkeypatch.setattr(feetech, "connect_follower", lambda port, cid: opened.append(port) or ClosingBus())
    assert main(["--host", "0.0.0.0"], serve=Recorder()) != 0
    assert opened == []


def test_a_non_loopback_host_needs_a_token(capsys):
    serve = Recorder()
    assert main(["--mock", "--host", "0.0.0.0"], serve=serve) != 0
    assert serve.calls == []
    assert "token" in capsys.readouterr().err.lower()


def test_a_non_loopback_host_with_a_token_is_allowed(monkeypatch):
    monkeypatch.setenv("ROBOTD_TOKEN", "s3cret")
    serve = Recorder()
    assert main(["--mock", "--host", "169.254.10.2"], serve=serve) == 0
    assert [(host, port) for _, host, port in serve.calls] == [("169.254.10.2", 8100)]


def test_mock_serves_a_working_app_on_localhost_8100():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    serve = Recorder()
    assert main(["--mock"], serve=serve) == 0
    [(app, host, port)] = serve.calls
    assert (host, port) == ("127.0.0.1", 8100)
    assert TestClient(app).get("/state").status_code == 200


def test_controller_takes_max_step_from_config():
    bus = MockBus()
    ctl = build_controller({"safety": {"max_step_deg": 5, "max_joint_speed_dps": 1000}}, bus)
    ctl.move_joints([20, 0, 0, 0, 0, 100], speed_dps=1000)
    assert goals(bus) == pytest.approx([5, 10, 15, 20])
