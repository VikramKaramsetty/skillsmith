"""WS1.4: robotd cameras: newest frame wins, capture timestamps, stale -> 503 (fake captures only)."""

import threading
import time

import pytest

from robotd.cameras import CaptureCamera
from robotd.cameras import FFmpegCapture as RealFFmpegCapture  # bound before conftest fakes it
from robotd.controller import ArmController, RobotdError
from robotd.mock import MockBus, MockCamera
from robotd.server import create_app
from skillsmith.perception.frames import MjpegParser


def jpeg(tag) -> bytes:
    return b"\xff\xd8" + str(tag).encode() + b"\xff\xd9"


class FakeCapture:
    """Delivers the given JPEGs, one per read (`gap_s` apart), then nothing; records close()."""

    def __init__(self, frames, gap_s=0.005, fail=False):
        self.frames = list(frames)
        self.gap_s = gap_s
        self.fail = fail
        self.closed = False

    def read(self):
        time.sleep(self.gap_s)
        if self.fail:
            raise OSError("select() timeout")
        return self.frames.pop(0) if self.frames else None

    def close(self):
        self.closed = True


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def camera(*captures, stale_s=1.0, reopen_s=0.01):
    queue = list(captures)
    cam = CaptureCamera("front", lambda: queue.pop(0) if queue else FakeCapture([]), stale_s=stale_s,
                        reopen_s=reopen_s)
    return cam


def test_the_newest_frame_wins():
    cam = camera(FakeCapture([jpeg(i) for i in range(1, 6)]))
    try:
        assert wait_for(lambda: cam.frames_captured >= 5)
        assert cam.latest()[0] == jpeg(5)
    finally:
        cam.close()


def test_each_frame_carries_its_capture_time():
    cam = camera(FakeCapture([jpeg(1)]))
    try:
        before = time.time()
        assert wait_for(lambda: cam.frames_captured >= 1)
        _, t = cam.latest()
        assert before - 0.1 <= t <= time.time()
    finally:
        cam.close()


def test_a_camera_that_stops_delivering_answers_503_not_a_stale_frame():
    cam = camera(FakeCapture([jpeg(1)]), stale_s=0.15)
    try:
        assert wait_for(lambda: cam.frames_captured >= 1)
        time.sleep(0.25)
        with pytest.raises(RobotdError) as e:
            cam.latest()
        assert e.value.status == 503 and "front" in e.value.message
    finally:
        cam.close()


def test_a_camera_that_never_delivered_says_why():
    cam = CaptureCamera("front", lambda: FakeCapture([], fail=True), stale_s=0.1, reopen_s=0.01)
    try:
        time.sleep(0.15)
        with pytest.raises(RobotdError) as e:
            cam.latest()
        assert e.value.status == 503 and "select() timeout" in e.value.message
    finally:
        cam.close()


def test_a_failing_capture_is_reopened():
    broken, fresh = FakeCapture([], fail=True), FakeCapture([jpeg("after-reopen")])
    cam = camera(broken, fresh)
    try:
        assert wait_for(lambda: cam.frames_captured >= 1)
        assert cam.latest()[0] == jpeg("after-reopen")
        assert broken.closed
    finally:
        cam.close()


def test_close_stops_the_thread_and_releases_the_capture():
    capture = FakeCapture([jpeg(1)])
    cam = camera(capture)
    assert wait_for(lambda: cam.frames_captured >= 1)
    cam.close()
    assert capture.closed
    assert not any(t.name == "camera front" for t in threading.enumerate())


# ---- over HTTP ---------------------------------------------------------------------------
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def client(cameras, **kw):
    return TestClient(create_app(ArmController(MockBus()), cameras, **kw))


def test_frame_has_a_capture_time_header_that_moves_forward():
    cam = camera(FakeCapture([jpeg(i) for i in range(200)], gap_s=0.01))
    try:
        c = client({"front": cam})
        assert wait_for(lambda: cam.frames_captured >= 1)
        first = c.get("/frame/front")
        time.sleep(0.05)
        second = c.get("/frame/front")
        assert first.status_code == second.status_code == 200
        assert float(second.headers["X-Frame-Time"]) > float(first.headers["X-Frame-Time"])
    finally:
        cam.close()


def test_stale_camera_frame_is_503_over_http():
    cam = camera(FakeCapture([]), stale_s=0.05)
    try:
        r = client({"front": cam}).get("/frame/front")
        assert r.status_code == 503 and r.json()["ok"] is False
    finally:
        cam.close()


def test_mock_frames_carry_a_capture_time_too():
    r = client({"front": MockCamera("front")}).get("/frame/front")
    assert abs(float(r.headers["X-Frame-Time"]) - time.time()) < 5


def test_stream_parts_parse_with_the_world_models_parser_and_carry_times():
    body = client({"wrist": MockCamera("wrist")}, stream_max_frames=3).get("/stream/wrist").content
    parts = MjpegParser().feed(body)
    assert len(parts) == 3
    times = [float(headers["x-frame-time"]) for headers, _ in parts]
    assert times == sorted(times)
    assert all(body.startswith(b"\xff\xd8") for _, body in parts)


def test_stream_ends_when_the_camera_goes_stale():
    cam = camera(FakeCapture([]), stale_s=0.05)
    try:
        r = client({"front": cam}, stream_max_frames=5).get("/stream/front")
        assert r.status_code == 200 and MjpegParser().feed(r.content) == []
    finally:
        cam.close()


LISTING = """[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] MacBook Pro Camera
[AVFoundation indev @ 0x1] [1] Vikram’s iPhone Camera
[AVFoundation indev @ 0x1] [2] USB2.0_CAM1
[AVFoundation indev @ 0x1] [3] Capture screen 0
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] Vikram’s iPhone Microphone
"""


def test_camera_names_resolve_to_avfoundation_indices():
    from robotd.cameras import parse_avfoundation_devices, resolve_source

    devices = parse_avfoundation_devices(LISTING)
    assert devices == [(0, "MacBook Pro Camera"), (1, "Vikram’s iPhone Camera"), (2, "USB2.0_CAM1"),
                       (3, "Capture screen 0")]
    listed = lambda: devices  # noqa: E731
    assert resolve_source("name:iPhone", listed) == 1
    assert resolve_source("name:vikram's iphone", listed) == 1  # straight quote matches the curly one
    assert resolve_source("name:USB2.0", listed) == 2
    assert resolve_source(0, listed) == 0 and resolve_source("http://x/stream", listed) == "http://x/stream"


def test_an_unknown_camera_name_lists_what_is_there():
    import pytest

    from robotd.cameras import parse_avfoundation_devices, resolve_source

    devices = parse_avfoundation_devices(LISTING)
    with pytest.raises(OSError, match=r"no camera named like 'name:Capture'.*\[1\] Vikram"):
        resolve_source("name:Capture", lambda: devices)  # screens are never cameras


# ---- name: cameras through ffmpeg (fake process on a real pipe; no camera is opened) -------------

class PipeProc:
    """Stands in for the ffmpeg process: the test writes bytes to `feed`, the capture reads stdout."""

    def __init__(self):
        import os

        r, w = os.pipe()
        self.stdout = os.fdopen(r, "rb", buffering=0)
        self._w = os.fdopen(w, "wb", buffering=0)
        self.cmd = None
        self.terminated = False

    def feed(self, data: bytes):
        self._w.write(data)

    def eof(self):
        self._w.close()

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def ffmpeg_capture(monkeypatch, timeout_s=0.2):
    from robotd import cameras

    monkeypatch.setattr(cameras, "_ffmpeg", lambda: "/bin/ffmpeg")
    proc = PipeProc()

    def popen(cmd, **kw):
        proc.cmd = cmd
        return proc

    devices = lambda: [(0, "MacBook Pro Camera"), (1, "Rohan’s iPhone Camera")]  # noqa: E731
    return RealFFmpegCapture("name:iPhone", (1280, 720), devices=devices, popen=popen, timeout_s=timeout_s), proc


def test_ffmpeg_opens_the_camera_by_its_full_name_not_an_index(monkeypatch):
    cap, proc = ffmpeg_capture(monkeypatch)
    assert cap.name == "Rohan’s iPhone Camera"
    i = proc.cmd.index("-i")
    assert proc.cmd[i + 1] == "Rohan’s iPhone Camera:none"
    assert "scale=1280:720" in proc.cmd and proc.cmd[-1] == "-"


def test_ffmpeg_capture_returns_the_newest_whole_jpeg(monkeypatch):
    cap, proc = ffmpeg_capture(monkeypatch)
    proc.feed(jpeg(1) + jpeg(2) + b"\xff\xd8partial")
    assert cap.read() == jpeg(2)
    proc.feed(b"-rest\xff\xd9")
    assert cap.read() == b"\xff\xd8partial-rest\xff\xd9"


def test_ffmpeg_capture_gives_none_when_ffmpeg_is_silent_or_gone(monkeypatch):
    cap, proc = ffmpeg_capture(monkeypatch)
    assert cap.read() is None  # timeout: CaptureCamera counts it and reopens after a few
    proc.eof()
    assert cap.read() is None
    cap.close()
    assert proc.terminated


def test_open_capture_uses_ffmpeg_for_names_on_macos_only(monkeypatch, never_open_real_cameras):
    from robotd import cameras

    monkeypatch.setattr(cameras, "_ffmpeg", lambda: "/bin/ffmpeg")
    picked = []
    monkeypatch.setattr(cameras, "FFmpegCapture", lambda s, z: picked.append(("ffmpeg", s)))
    monkeypatch.setattr(cameras, "OpenCVCapture", lambda s, z: picked.append(("opencv", s)))
    monkeypatch.setattr(cameras.sys, "platform", "darwin")
    cameras.open_capture("name:iPhone")
    cameras.open_capture(0)
    monkeypatch.setattr(cameras.sys, "platform", "linux")
    cameras.open_capture("name:iPhone")
    assert picked == [("ffmpeg", "name:iPhone"), ("opencv", 0), ("opencv", "name:iPhone")]
