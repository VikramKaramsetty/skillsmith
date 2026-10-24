"""WS1.1: `python -m robotd` argument handling (uvicorn is replaced by a recorder)."""

import pytest
from conftest import goals

from robotd.__main__ import build_controller, main
from robotd.mock import MockBus


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, app, host, port):
        self.calls.append((app, host, port))


@pytest.fixture(autouse=True)
def no_token_env(monkeypatch):
    monkeypatch.delenv("ROBOTD_TOKEN", raising=False)


def test_without_mock_it_refuses_until_the_feetech_backend_exists(capsys):
    serve = Recorder()
    assert main([], serve=serve) == 1
    assert serve.calls == []
    assert "--mock" in capsys.readouterr().err


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
