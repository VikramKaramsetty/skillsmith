"""WS3 test fakes: fixture paths, stand-in JPEGs and a stand-in robotd HTTP server (stdlib only)."""

import json
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

FIXTURES = Path(__file__).with_name("fixtures")


def jpeg(tag: str) -> bytes:
    """A tiny stand-in JPEG: SOI, a payload, EOI (frame sources never decode unless asked)."""
    return b"\xff\xd8" + tag.encode() + b"\xff\xd9"


class FakeRobotd:
    """What the handler serves; tests change it between requests."""

    def __init__(self):
        self.frame = jpeg("front-1")
        self.frame_time = None  # value for the X-Frame-Time header, or None to leave it out
        self.token = None  # required X-Robotd-Token, or None
        self.stream_parts = [jpeg("s1"), jpeg("s2"), jpeg("s3")]
        self.stream_lengths = True  # send Content-Length per part
        self.stream_hold_s = 2.0  # keep the stream open after the last part
        self.stream_t0 = None  # X-Frame-Time of part i is stream_t0 + i; None: the send time, "omit": no header
        self.stream_refusal = None  # (code, error) to refuse /stream with, e.g. robotd's 503 for a dead camera
        self.stream_connections = 0
        self.url = ""


def robotd_handler(state: FakeRobotd):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _refuse(self, code, error):
            body = json.dumps({"ok": False, "error": error}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if state.token and self.headers.get("X-Robotd-Token") != state.token:
                return self._refuse(401, "missing or wrong X-Robotd-Token header")
            if self.path == "/frame/front":
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                if state.frame_time is not None:
                    self.send_header("X-Frame-Time", str(state.frame_time))
                self.send_header("Content-Length", str(len(state.frame)))
                self.end_headers()
                self.wfile.write(state.frame)
            elif self.path == "/stream/front":
                state.stream_connections += 1
                if state.stream_refusal:
                    return self._refuse(*state.stream_refusal)
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                for i, part in enumerate(state.stream_parts):
                    head = "--frame\r\nContent-Type: image/jpeg\r\n"
                    if state.stream_lengths:
                        head += f"Content-Length: {len(part)}\r\n"
                    if state.stream_t0 != "omit":
                        stamp = time.time() if state.stream_t0 is None else state.stream_t0 + i
                        head += f"X-Frame-Time: {stamp}\r\n"
                    head += "\r\n"
                    self.wfile.write(head.encode() + part + b"\r\n")
                    self.wfile.flush()
                time.sleep(state.stream_hold_s)
            else:
                self._refuse(404, f"no camera '{self.path.rsplit('/', 1)[-1]}' (have: front)")

    return Handler
