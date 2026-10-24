"""Frame sources: where camera frames come from. OWNER: WS3 (WS3.1).

Contract:
- `Frame(jpeg, t, source)`: one camera frame as JPEG bytes, its capture time (unix seconds) and
  where it came from ("robotd:front", "mjpeg:<url>", "opencv:<device>", "fixture:<file>").
  `frame.bgr()` decodes it (cv2/numpy, imported lazily), so moving frames around needs no OpenCV.
- `FrameSource`: `read() -> Frame` (the latest frame) and `close()`. Every failure is a `FrameError`
  with a readable reason; the WorldModel turns it into an event.
- `RobotdFrames(robotd_url, cam)`: GET /frame/{cam} per read. This is how the orchestrator sees, since
  only robotd opens the cameras. The capture time comes from robotd's `X-Frame-Time` header when present
  (WS1.4), else the receipt time. Sends `X-Robotd-Token` from $ROBOTD_TOKEN when set.
- `MjpegFrames(url)`: a background reader on a multipart MJPEG stream (robotd /stream/{cam}, or the
  MacBook stand-in camera) that keeps only the latest frame and reconnects when the stream drops.
  `read()` raises `StaleFrameError` (a `FrameError`) naming the last stream error once the newest frame
  is older than `max_age_s` (default 1.0; None turns the check off), so a dead camera never looks like a
  frozen table. A frame's age uses its `X-Frame-Time` (robotd's capture time) when present, else the
  receipt time. Given a `bus`, each failing episode also emits one `error` event (where "perception.frames").
- `OpenCVFrames(device)`: a local camera or image sequence via cv2.VideoCapture. On the GX10 only robotd
  may use it (it owns /dev/video*); elsewhere it is for a laptop webcam or recorded files.
- `FixtureFrames(manifest)`: recorded frames listed in a JSON manifest {"frames": [{"file", "t"}, ...]},
  e.g. tests/ws3/fixtures/truth.json or what `record()` writes.
- `record(source, out_dir, count, every_s)`: save frames plus manifest.json, to capture real fixtures:
  `python -m skillsmith.perception.frames --cam front --count 10 --out tests/ws3/fixtures/real`.
- `make_frame_source(cfg, cam)`: the WorldModel's default source (robotd at cfg.robot.robotd_url).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, Union

SOI, EOI = b"\xff\xd8", b"\xff\xd9"  # JPEG start / end of image markers
TOKEN_HEADER = "X-Robotd-Token"  # same header robotd checks (robotd/server.py)
TIME_HEADER = "X-Frame-Time"
TOKEN_ENV = "ROBOTD_TOKEN"
JPEG_QUALITY = 90

# robotd is on localhost or a link-local cable: never route it through an HTTP proxy from the environment
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FrameError(RuntimeError):
    """No usable frame, with the reason."""


class StaleFrameError(FrameError):
    """The newest frame is too old: the stream died or stalled. `age_s` and `max_age_s` in seconds."""

    def __init__(self, message: str, age_s: float, max_age_s: float):
        super().__init__(message)
        self.age_s = age_s
        self.max_age_s = max_age_s


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    t: float
    source: str

    def __post_init__(self):
        if not self.jpeg.startswith(SOI):
            raise FrameError(f"{self.source}: not a JPEG (starts {self.jpeg[:12]!r})")

    def bgr(self):
        """The decoded image, H x W x 3 uint8 in BGR order."""
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(self.jpeg, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise FrameError(f"{self.source}: JPEG could not be decoded")
        return image


class FrameSource(Protocol):
    def read(self) -> Frame: ...

    def close(self) -> None: ...


def _request(url: str, token: Optional[str]) -> urllib.request.Request:
    return urllib.request.Request(url, headers={TOKEN_HEADER: token} if token else {})


def _refusal(e: urllib.error.HTTPError) -> str:
    try:
        return f"{e.code} {json.loads(e.read().decode())['error']}"
    except Exception:
        return f"{e.code} {e.reason}"


class RobotdFrames:
    """One GET /frame/{cam} per read: always the newest frame, no thread."""

    def __init__(self, robotd_url: str, cam: str, token: Optional[str] = None, timeout_s: float = 2.0):
        self.cam = cam
        self.url = f"{robotd_url.rstrip('/')}/frame/{cam}"
        self.token = token
        self.timeout_s = timeout_s

    def read(self) -> Frame:
        try:
            with _OPENER.open(_request(self.url, self.token), timeout=self.timeout_s) as response:
                body = response.read()
                stamp = response.headers.get(TIME_HEADER)
        except urllib.error.HTTPError as e:
            raise FrameError(f"robotd refused {self.url}: {_refusal(e)}") from e
        except (urllib.error.URLError, OSError) as e:
            raise FrameError(f"cannot reach robotd at {self.url}: {getattr(e, 'reason', e)}") from e
        return Frame(body, float(stamp) if stamp else time.time(), f"robotd:{self.cam}")

    def close(self) -> None:
        pass


class MjpegParser:
    """Splits a multipart MJPEG byte stream into (headers, jpeg) parts, fed in arbitrary chunks.

    Uses each part's Content-Length when given, else the JPEG start/end markers.
    Header names come back lower-case.
    """

    def __init__(self):
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> List[Tuple[Dict[str, str], bytes]]:
        self._buf += chunk
        parts = []
        while True:
            start = self._buf.find(SOI)
            if start < 0:
                return parts
            headers = _headers(bytes(self._buf[:start]))
            if "content-length" in headers:
                end = start + int(headers["content-length"])
                if len(self._buf) < end:
                    return parts
            else:
                eoi = self._buf.find(EOI, start + 2)
                if eoi < 0:
                    return parts
                end = eoi + 2
            parts.append((headers, bytes(self._buf[start:end])))
            del self._buf[:end]


def _headers(block: bytes) -> Dict[str, str]:
    lines = block.decode("latin-1").splitlines()
    return {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines if ":" in line)}


class MjpegFrames:
    """Reads an MJPEG stream in a daemon thread; `read()` returns the latest frame while it is fresh."""

    def __init__(self, url: str, token: Optional[str] = None, timeout_s: float = 2.0, reconnect_s: float = 1.0,
                 max_age_s: Optional[float] = 1.0, bus: Any = None):
        self.url = url
        self.token = token
        self.timeout_s = timeout_s
        self.reconnect_s = reconnect_s
        self.max_age_s = max_age_s
        self.bus = bus
        self.frames_received = 0
        self._latest: Optional[Frame] = None
        self._response = None
        self._error = "no frame yet"
        self._failing = False  # an error event was emitted for the current failing episode
        self._ready = threading.Condition()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"mjpeg {url}", daemon=True)
        self._thread.start()

    def read(self) -> Frame:
        try:
            frame = self._fresh()
        except FrameError as e:
            if not self._failing:
                self._failing = True
                _emit(self.bus, str(e))
            raise
        self._failing = False
        return frame

    def _fresh(self) -> Frame:
        with self._ready:
            if self._latest is None:
                self._ready.wait_for(lambda: self._latest is not None, timeout=self.timeout_s)
            frame, error = self._latest, self._error
        if frame is None:
            raise FrameError(f"no frame from {self.url} within {self.timeout_s}s: {error}")
        age = time.time() - frame.t
        if self.max_age_s is not None and age > self.max_age_s:
            why = f"last stream error: {error}" if self._response is None else "stream open but silent"
            raise StaleFrameError(f"stale frame from {self.url}: newest is {age:.2f}s old "
                                  f"(max {self.max_age_s}s); {why}", age, self.max_age_s)
        return frame

    def close(self) -> None:
        self._closed.set()
        response = self._response
        if response is not None:
            try:  # wake the reader's blocked read now instead of after the socket timeout
                response.fp.raw._sock.shutdown(socket.SHUT_RDWR)  # http.client keeps the socket private
            except Exception:
                pass
        self._thread.join(timeout=self.timeout_s + 1)

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                with _OPENER.open(_request(self.url, self.token), timeout=self.timeout_s) as response:
                    self._response = response
                    parser = MjpegParser()
                    while not self._closed.is_set():
                        chunk = response.read1(65536)
                        if not chunk:
                            self._error = "stream ended"
                            break
                        for headers, jpeg in parser.feed(chunk):
                            self._store(headers, jpeg)
            except urllib.error.HTTPError as e:
                self._error = _refusal(e)
            except Exception as e:  # dropped link, timeout, bad part: note why, wait, reconnect
                self._error = f"{type(e).__name__}: {e}"
            self._response = None
            self._closed.wait(self.reconnect_s)

    def _store(self, headers: Dict[str, str], jpeg: bytes) -> None:
        stamp = headers.get(TIME_HEADER.lower())
        try:
            frame = Frame(jpeg, float(stamp) if stamp else time.time(), f"mjpeg:{self.url}")
        except FrameError as e:
            self._error = str(e)
            return
        with self._ready:
            self._latest = frame
            self.frames_received += 1
            self._ready.notify_all()


def _emit(bus: Any, message: str) -> None:
    if bus is not None:
        bus.emit("error", where="perception.frames", message=message)
    else:
        print(f"[perception.frames] {message}")


class OpenCVFrames:
    """cv2.VideoCapture on a device index, device path, video file or image-sequence pattern."""

    def __init__(self, device: Union[int, str], size: Optional[Tuple[int, int]] = None):
        import cv2

        self._cv2 = cv2
        self.device = device
        self._capture = cv2.VideoCapture(device)
        if not self._capture.isOpened():
            raise FrameError(f"cannot open camera {device!r}")
        if size:
            self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
            self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])

    def read(self) -> Frame:
        ok, image = self._capture.read()
        t = time.time()
        if not ok:
            raise FrameError(f"camera {self.device!r} returned no frame")
        ok, buf = self._cv2.imencode(".jpg", image, [self._cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            raise FrameError(f"camera {self.device!r}: JPEG encoding failed")
        return Frame(buf.tobytes(), t, f"opencv:{self.device}")

    def close(self) -> None:
        self._capture.release()


class FixtureFrames:
    """Recorded frames from a JSON manifest, in order (optionally looping)."""

    def __init__(self, manifest: Union[str, Path], loop: bool = False):
        path = Path(manifest)
        entries = json.loads(path.read_text())["frames"]
        self._frames = [(path.parent / e["file"], float(e["t"])) for e in entries]
        self._manifest = path
        self._loop = loop
        self._next = 0

    def read(self) -> Frame:
        if self._next >= len(self._frames):
            if not self._loop or not self._frames:
                raise FrameError(f"no more fixture frames in {self._manifest}")
            self._next = 0
        file, t = self._frames[self._next]
        self._next += 1
        return Frame(file.read_bytes(), t, f"fixture:{file.name}")

    def close(self) -> None:
        pass


def record(source: FrameSource, out_dir: Union[str, Path], count: int, every_s: float = 0.5) -> Path:
    """Save `count` frames as frame_000.jpg... plus manifest.json (readable by FixtureFrames)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    entries = []
    for i in range(count):
        frame = source.read()
        name = f"frame_{i:03d}.jpg"
        (out / name).write_bytes(frame.jpeg)
        entries.append({"file": name, "t": frame.t, "source": frame.source})
        if i + 1 < count:
            time.sleep(every_s)
    manifest = out / "manifest.json"
    manifest.write_text(json.dumps({"frames": entries}, indent=1) + "\n")
    return manifest


def make_frame_source(cfg, cam: str) -> RobotdFrames:
    return RobotdFrames(cfg.robot.robotd_url, cam, token=os.environ.get(TOKEN_ENV) or None)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m skillsmith.perception.frames",
                                description="Record camera frames from robotd as fixtures.")
    p.add_argument("--cam", default="front", help="front | wrist")
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--every", type=float, default=0.5, help="seconds between frames")
    p.add_argument("--out", required=True)
    p.add_argument("--url", help="robotd URL (default: config robot.robotd_url)")
    args = p.parse_args(argv)
    if args.url:
        source = RobotdFrames(args.url, args.cam, token=os.environ.get(TOKEN_ENV) or None)
    else:
        from skillsmith.config import load_config

        source = make_frame_source(load_config(), args.cam)
    print(record(source, args.out, args.count, args.every))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
