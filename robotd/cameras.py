"""Real cameras for robotd: the only process that opens them. OWNER: WS1 (WS1.4).

`CaptureCamera(name, open_capture)` runs one daemon thread that reads frames as fast as the camera
gives them (~15 fps) and keeps only the newest JPEG plus its capture time (unix seconds).
`latest() -> (jpeg, t)` raises RobotdError 503 with the reason when no frame is newer than `stale_s`,
so a dead camera never hands the world model an old frame. A capture that keeps failing is closed
and reopened every `reopen_s`.

`open_capture(source, size)` picks the capture: on macOS a `name:<text>` camera is read through ffmpeg by
its exact device name (`FFmpegCapture`), because OpenCV's camera indices don't follow ffmpeg's list order.
`OpenCVCapture(source, size)` reads a device index, a device path, an http MJPEG URL or `name:<text>`
through cv2.VideoCapture (imported lazily) and returns JPEG bytes. Tests replace it; agents never open a
camera. `name:iPhone` picks the macOS camera whose name contains "iPhone" (Continuity Camera): indices
shift as Continuity and USB cameras come and go, names don't. The name -> index map comes from ffmpeg's
AVFoundation device list (the same order OpenCV's AVFoundation backend uses); robotd logs it at start.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, List, Optional, Protocol, Tuple, Union

from robotd.controller import RobotdError

STALE_S = 1.0
FAILS_BEFORE_REOPEN = 5
JPEG_QUALITY = 85
FFMPEG_FPS = 30

log = logging.getLogger("robotd")


class Capture(Protocol):
    def read(self) -> Optional[bytes]:
        """The next frame as JPEG bytes, or None when the camera gave nothing."""

    def close(self) -> None: ...


_DEVICE_LINE = re.compile(r"\[(\d+)\] (.+)$")


def _ffmpeg() -> Optional[str]:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return shutil.which("ffmpeg")


def parse_avfoundation_devices(listing: str) -> List[Tuple[int, str]]:
    """The video devices from `ffmpeg -f avfoundation -list_devices true -i ""` stderr, in index order."""
    devices, video = [], False
    for line in listing.splitlines():
        if "AVFoundation video devices" in line:
            video = True
        elif "AVFoundation audio devices" in line:
            break
        elif video and (m := _DEVICE_LINE.search(line)):
            devices.append((int(m.group(1)), m.group(2).strip()))
    return devices


def avfoundation_devices() -> List[Tuple[int, str]]:
    """macOS camera indices and names (lists devices only; opens nothing)."""
    exe = _ffmpeg()
    if not exe:
        raise OSError("camera names need ffmpeg: pip install imageio-ffmpeg in robotd's venv")
    out = subprocess.run([exe, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                         capture_output=True, text=True, timeout=15)
    return parse_avfoundation_devices(out.stderr)


def _norm(text: str) -> str:
    return text.replace("\u2019", "'").casefold()


def resolve_source(source: Union[int, str],
                   devices: Optional[Callable[[], List[Tuple[int, str]]]] = None) -> Union[int, str]:
    """`name:<text>` -> the index of the first camera whose name contains <text> (case-insensitive);
    anything else is returned as it is."""
    if not (isinstance(source, str) and source.startswith("name:")):
        return source
    want = _norm(source[5:].strip())
    found = (devices or avfoundation_devices)()
    for index, name in found:
        if want in _norm(name) and not name.startswith("Capture screen"):
            log.info("camera %r is index %d (%s)", source, index, name)
            return index
    names = ", ".join(f"[{i}] {n}" for i, n in found) or "none"
    raise OSError(f"no camera named like {source!r}; cameras: {names}")


def device_name(source: str, devices: Optional[Callable[[], List[Tuple[int, str]]]] = None) -> str:
    """`name:<text>` -> the full macOS name of the first camera whose name contains <text>."""
    found = (devices or avfoundation_devices)()
    index = resolve_source(source, lambda: found)
    return dict(found)[index]


def ffmpeg_command(exe: str, name: str, size: Optional[Tuple[int, int]] = None) -> List[str]:
    """ffmpeg reading one AVFoundation camera by its exact name and writing JPEG frames to stdout."""
    scale = ["-vf", f"scale={size[0]}:{size[1]}"] if size else []
    return [exe, "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-framerate", str(FFMPEG_FPS),
            "-i", f"{name}:none", *scale, "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "3", "-"]


class FFmpegCapture:
    """A `name:` camera read through ffmpeg by its exact device name (macOS only).

    OpenCV's AVFoundation backend numbers cameras in its own order, which differs from ffmpeg's
    device list once an iPhone is plugged in by USB (robotd opened the MacBook camera as the iPhone).
    Opening by name means no index is involved at all."""

    def __init__(self, source: str, size: Optional[Tuple[int, int]] = None,
                 devices: Optional[Callable[[], List[Tuple[int, str]]]] = None,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen, timeout_s: float = 3.0):
        exe = _ffmpeg()
        if not exe:
            raise OSError("name: cameras need ffmpeg: pip install imageio-ffmpeg in robotd's venv")
        self.source = source
        self.name = device_name(source, devices)
        self.timeout_s = timeout_s
        self._buf = b""
        self._proc = popen(ffmpeg_command(exe, self.name, size), stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        log.info("camera %r opened by name through ffmpeg (%s)", source, self.name)

    def read(self) -> Optional[bytes]:
        """The newest complete JPEG ffmpeg has written, or None after `timeout_s` without one."""
        deadline = time.monotonic() + self.timeout_s
        while True:
            frame = self._take_newest()
            if frame is not None:
                return frame
            left = deadline - time.monotonic()
            if left <= 0 or not self._fill(left):
                return None

    def _fill(self, wait_s: float) -> bool:
        import os
        import select

        out = self._proc.stdout
        ready, _, _ = select.select([out], [], [], wait_s)
        if not ready:
            return False
        chunk = os.read(out.fileno(), 1 << 20)
        self._buf += chunk
        return bool(chunk)  # b"" = ffmpeg exited

    def _take_newest(self) -> Optional[bytes]:
        end = self._buf.rfind(b"\xff\xd9")
        if end < 0:
            return None
        start = self._buf.rfind(b"\xff\xd8", 0, end)
        if start < 0:
            return None
        frame, self._buf = self._buf[start:end + 2], self._buf[end + 2:]
        return frame

    def close(self) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._proc.kill()


def open_capture(source: Union[int, str], size: Optional[Tuple[int, int]] = None) -> Capture:
    """`name:` cameras through ffmpeg by name on macOS; anything else through OpenCV."""
    if isinstance(source, str) and source.startswith("name:") and sys.platform == "darwin" and _ffmpeg():
        return FFmpegCapture(source, size)
    return OpenCVCapture(source, size)


class OpenCVCapture:
    def __init__(self, source: Union[int, str], size: Optional[Tuple[int, int]] = None):
        import cv2

        self._cv2 = cv2
        self.source = source
        self._capture = cv2.VideoCapture(resolve_source(source))
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
