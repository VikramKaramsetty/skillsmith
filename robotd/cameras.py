"""Real cameras for robotd: the only process that opens them. OWNER: WS1 (WS1.4).

`CaptureCamera(name, open_capture)` runs one daemon thread that reads frames as fast as the camera
gives them (~15 fps) and keeps only the newest JPEG plus its capture time (unix seconds).
`latest() -> (jpeg, t)` raises RobotdError 503 with the reason when no frame is newer than `stale_s`,
so a dead camera never hands the world model an old frame. A capture that keeps failing is closed
and reopened every `reopen_s`.

`OpenCVCapture(source, size)` reads a V4L device path/index or an http MJPEG URL through
cv2.VideoCapture (imported lazily) and returns JPEG bytes. Tests replace it; agents never open a camera.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional, Protocol, Tuple, Union

from robotd.controller import RobotdError

STALE_S = 1.0
FAILS_BEFORE_REOPEN = 5
JPEG_QUALITY = 85

log = logging.getLogger("robotd")


class Capture(Protocol):
    def read(self) -> Optional[bytes]:
        """The next frame as JPEG bytes, or None when the camera gave nothing."""

    def close(self) -> None: ...


class OpenCVCapture:
    def __init__(self, source: Union[int, str], size: Optional[Tuple[int, int]] = None):
        import cv2

        self._cv2 = cv2
        self.source = source
        self._capture = cv2.VideoCapture(source)
        if size:
            self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
            self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
        if not self._capture.isOpened():
            raise OSError(f"cannot open camera {source!r}")

    def read(self) -> Optional[bytes]:
        ok, image = self._capture.read()
        if not ok:
            return None
        ok, buf = self._cv2.imencode(".jpg", image, [self._cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        return buf.tobytes() if ok else None

    def close(self) -> None:
        self._capture.release()


class CaptureCamera:
    def __init__(self, name: str, open_capture: Callable[[], Capture], stale_s: float = STALE_S,
                 reopen_s: float = 1.0):
        self.name = name
        self.stale_s = stale_s
        self.reopen_s = reopen_s
        self.frames_captured = 0
        self._open = open_capture
        self._lock = threading.Lock()
        self._latest: Optional[Tuple[bytes, float]] = None
        self._error = "no frame yet"
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"camera {name}", daemon=True)
        self._thread.start()

    def latest(self) -> Tuple[bytes, float]:
        with self._lock:
            latest, error = self._latest, self._error
        if latest is None:
            raise RobotdError(503, f"camera '{self.name}' has not delivered a frame: {error}")
        age = time.time() - latest[1]
        if age > self.stale_s:
            raise RobotdError(503, f"camera '{self.name}' has delivered no frame for {age:.1f} s: {error}")
        return latest

    def close(self) -> None:
        self._closed.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._closed.is_set():
            capture = self._try_open()
            if capture is None:
                self._closed.wait(self.reopen_s)
                continue
            try:
                self._read_until_failing(capture)
            finally:
                capture.close()
            self._closed.wait(self.reopen_s)

    def _try_open(self) -> Optional[Capture]:
        try:
            return self._open()
        except Exception as e:
            self._note(f"cannot open: {type(e).__name__}: {e}")
            return None

    def _read_until_failing(self, capture: Capture) -> None:
        fails, why = 0, "the camera returned no image"
        while not self._closed.is_set() and fails < FAILS_BEFORE_REOPEN:
            try:
                jpeg = capture.read()
            except Exception as e:
                jpeg, why = None, f"{type(e).__name__}: {e}"
            if jpeg is None:
                fails += 1
                continue
            fails = 0
            with self._lock:
                self._latest = (jpeg, time.time())
                self.frames_captured += 1
        if fails:
            self._note(f"{why} ({FAILS_BEFORE_REOPEN} reads in a row); reopening")

    def _note(self, error: str) -> None:
        with self._lock:
            changed = error != self._error
            self._error = error
        if changed:
            log.warning("camera %s: %s", self.name, error)
