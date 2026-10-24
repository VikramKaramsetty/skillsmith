"""WS3.1: frame sources (robotd client, MJPEG, fixtures) without OpenCV."""

import json
import socket
import time

import pytest
from ws3_fakes import FIXTURES, jpeg

from skillsmith.perception.frames import (
    FixtureFrames,
    Frame,
    FrameError,
    MjpegFrames,
    MjpegParser,
    RobotdFrames,
    make_frame_source,
    record,
)


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---- Frame -------------------------------------------------------------------------------
def test_a_frame_must_be_a_jpeg():
    with pytest.raises(FrameError, match="not a JPEG"):
        Frame(b"<html>oops</html>", 1.0, "robotd:front")


# ---- RobotdFrames ------------------------------------------------------------------------
def test_robotd_frame_carries_its_capture_time(robotd):
    robotd.frame_time = 1730000000.25
    frame = RobotdFrames(robotd.url, "front").read()
    assert frame.jpeg == jpeg("front-1")
    assert frame.t == 1730000000.25
    assert frame.source == "robotd:front"


def test_without_a_capture_time_the_receipt_time_is_used(robotd):
    before = time.time()
    frame = RobotdFrames(robotd.url, "front").read()
    assert before <= frame.t <= time.time()


def test_each_read_is_a_fresh_frame(robotd):
    source = RobotdFrames(robotd.url, "front")
    source.read()
    robotd.frame = jpeg("front-2")
    assert source.read().jpeg == jpeg("front-2")


def test_the_token_is_sent(robotd):
    robotd.token = "s3cret"
    assert RobotdFrames(robotd.url, "front", token="s3cret").read().jpeg == jpeg("front-1")
    with pytest.raises(FrameError, match="401"):
        RobotdFrames(robotd.url, "front", token=None).read()


def test_robotd_refusal_is_reported_with_its_reason(robotd):
    with pytest.raises(FrameError, match="no camera 'side'"):
        RobotdFrames(robotd.url, "side").read()


def test_unreachable_robotd_is_a_frame_error():
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(FrameError, match="cannot reach robotd"):
        RobotdFrames(f"http://127.0.0.1:{port}", "front", timeout_s=0.5).read()


def test_a_non_jpeg_body_is_rejected(robotd):
    robotd.frame = b"not an image"
    with pytest.raises(FrameError, match="not a JPEG"):
        RobotdFrames(robotd.url, "front").read()


# ---- MJPEG -------------------------------------------------------------------------------
def test_parser_splits_parts_by_content_length_across_chunks():
    with_thumbnail = jpeg("thumb") + b"rest-of-image\xff\xd9"  # an end marker inside the part (EXIF thumbnail)
    stream = b"".join(f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(p)}\r\nX-Frame-Time: {t}\r\n\r\n"
                      .encode() + p + b"\r\n" for t, p in [(5.0, with_thumbnail), (6.0, jpeg("bb"))])
    parser = MjpegParser()
    parts = [part for i in range(0, len(stream), 7) for part in parser.feed(stream[i:i + 7])]
    assert [(h.get("x-frame-time"), body) for h, body in parts] == [("5.0", with_thumbnail), ("6.0", jpeg("bb"))]


def test_parser_falls_back_to_jpeg_markers_without_content_length():
    parser = MjpegParser()
    parts = parser.feed(b"--frame\r\n\r\n" + jpeg("x") + b"\r\n--frame\r\n\r\n" + jpeg("y")[:3])
    assert [body for _, body in parts] == [jpeg("x")]
    assert [body for _, body in parser.feed(jpeg("y")[3:] + b"\r\n")] == [jpeg("y")]


def test_mjpeg_reader_keeps_the_latest_frame(robotd):
    source = MjpegFrames(f"{robotd.url}/stream/front")
    try:
        assert wait_for(lambda: source.frames_received >= 3)
        frame = source.read()
        assert (frame.jpeg, frame.t) == (jpeg("s3"), 1002.0)
    finally:
        source.close()


def test_mjpeg_reader_handles_streams_without_content_length(robotd):
    robotd.stream_lengths = False
    source = MjpegFrames(f"{robotd.url}/stream/front")
    try:
        assert wait_for(lambda: source.frames_received >= 3)
        assert source.read().jpeg == jpeg("s3")
    finally:
        source.close()


def test_mjpeg_reader_reconnects_when_the_stream_ends(robotd):
    robotd.stream_hold_s = 0.0
    source = MjpegFrames(f"{robotd.url}/stream/front", reconnect_s=0.05)
    try:
        assert wait_for(lambda: robotd.stream_connections >= 2)
    finally:
        source.close()


def test_mjpeg_read_without_any_frame_times_out_with_the_reason(robotd):
    robotd.token = "s3cret"
    source = MjpegFrames(f"{robotd.url}/stream/front", timeout_s=0.3)
    try:
        with pytest.raises(FrameError, match="401"):
            source.read()
    finally:
        source.close()


# ---- fixtures and recording --------------------------------------------------------------
def test_fixture_frames_come_back_in_order_with_their_times():
    truth = json.loads((FIXTURES / "truth.json").read_text())
    source = FixtureFrames(FIXTURES / "truth.json")
    frames = [source.read() for _ in truth["frames"]]
    assert [f.source for f in frames] == [f"fixture:{e['file']}" for e in truth["frames"]]
    assert [f.t for f in frames] == [e["t"] for e in truth["frames"]]
    with pytest.raises(FrameError, match="no more"):
        source.read()


def test_fixture_frames_can_loop():
    source = FixtureFrames(FIXTURES / "truth.json", loop=True)
    first = source.read()
    for _ in range(len(json.loads((FIXTURES / "truth.json").read_text())["frames"]) - 1):
        source.read()
    assert source.read() == first


def test_record_then_replay_round_trips(robotd, tmp_path):
    robotd.frame_time = 42.5
    manifest = record(RobotdFrames(robotd.url, "front"), tmp_path / "rec", count=2, every_s=0)
    replay = FixtureFrames(manifest)
    assert [(f.jpeg, f.t) for f in (replay.read(), replay.read())] == [(jpeg("front-1"), 42.5)] * 2


class Cfg:
    class robot:
        robotd_url = "http://127.0.0.1:8100"


def test_world_model_frames_come_from_robotd():
    source = make_frame_source(Cfg, "wrist")
    assert isinstance(source, RobotdFrames)
    assert source.url == "http://127.0.0.1:8100/frame/wrist"
