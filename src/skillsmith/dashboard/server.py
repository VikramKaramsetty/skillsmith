"""Dashboard server. OWNER: WS6 (docs/workstreams/WS6_dashboard.md).

Contract:
- `main(cfg) -> int`: called by `skillsmith serve`; serves on :8080 and blocks until Ctrl-C.
- `GET /`: the single-page UI (one HTML file, no build step).
- `GET /events`: SSE stream of events (docs/INTERFACES.md §7) from the live EventBus.
  `GET /events?replay=<run_id>&speed=2` replays runs/<run_id>/events.jsonl (demo fallback).
- `POST /task {"text": "..."}`: parse the task and start a RepairLoop in a worker thread.
- `POST /approve {"approved": bool, "note": str, "nonce": str}`: resolves the pending DashboardApprover request.
- The UI renders ONLY from events. If it needs more data, request a new event field from the lead.

WS6.1 builds the event feed: the live hub, replay and the run list. WS6.3 adds `/task` (one run
at a time; 400 if the text doesn't parse, 409 while a run is going) and `/approve` (409 if
nothing waits; an optional `run_id` must match the waiting run so a stale page can't approve).
`GET /approval` shows what waits. WS6.4 adds `GET /frame?path=` so the detail drawer can show the
annotated key frames the events point at (images inside the runs dir only).
WS6.5 adds `GET /camera/{cam}` (robotd's MJPEG `/stream/{cam}` passed through, so the browser
only talks to the dashboard and only robotd holds the camera; 503 if robotd isn't reachable)
and `GET /video?path=` for the sim replay mp4 that `sim.video` points at (runs dir only).
WS6.6 adds `GET /improvement`: one summary per recorded run (dashboard/improvement.py), so the
improvement panel shows earlier runs; the page folds the live run itself.
WS6.7 adds speed control to replay: `GET /events?replay=<run_id>&speed=4&from=<n>` starts at the
n-th recorded event, so the page pauses (closes the stream), resumes or changes speed (reopens at
the next event) without the story starting over. Events and `replay.end` keep their shape (the page
counts what it got: the next index is from + events received), so replay stays the live stream.
WS6.8 locks down the two POSTs that can move the arm: `/approve` needs the per-request `nonce`
from `GET /approval` (never in an event); `/approve` and `/task` refuse a foreign Origin or a
cross-site Sec-Fetch-Site (403 + an `error` event) and anything but JSON (415). The server binds
127.0.0.1 (`dashboard.host` widens it) and refuses Host headers outside the allow-list (DNS rebinding).
FastAPI is imported lazily so this module imports with core deps only.
Replayed events carry `"replay": true` so the UI can label the fallback as a recording.
No `from __future__ import annotations` here: FastAPI must resolve the locally imported `Request`.
"""

import asyncio
import json
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import AsyncIterator, Callable, Iterator, Optional
from urllib.parse import urlsplit

from skillsmith.dashboard.approver import DashboardApprover
from skillsmith.events import EventBus, new_run_id
from skillsmith.tasks import parse_task
from skillsmith.types import TaskSpec

PORT = 8080
HOST = "127.0.0.1"  # loopback only; `dashboard.host` in the config opts in to a wider bind
MAX_REPLAY_GAP_S = 3.0  # long idle gaps in a recording are shortened to this (before speed)
HEARTBEAT_S = 15.0
HISTORY = 2000  # live events kept so a page opened mid-run still sees the whole story
INDEX_HTML = Path(__file__).with_name("index.html")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
VIDEO_TYPES = {".mp4": "video/mp4", ".webm": "video/webm"}
CAMERAS = ("front", "wrist")
CAMERA_TIMEOUT_S = 5.0  # connect/read timeout on robotd's stream, so a dead robotd frees the worker
CAMERA_CHUNK = 64 * 1024


class EventHub:
    """Fans live events out to SSE clients. `publish` is thread-safe (the loop runs in a worker)."""

    def __init__(self, history: int = HISTORY):
        self._lock = threading.Lock()
        self._history: deque[dict] = deque(maxlen=history)
        self._clients: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []

    def attach(self, bus) -> None:
        """Forward every event of an EventBus to the dashboard."""
        bus.subscribe(self.publish)

    def publish(self, event: dict) -> None:
        with self._lock:
            self._history.append(event)
            clients = list(self._clients)
        for loop, queue in clients:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:  # client's loop already closed; it unsubscribes itself
                pass

    def subscribe(self) -> tuple[list[dict], asyncio.Queue]:
        """Register the calling coroutine's loop. Returns (history so far, queue of new events)."""
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._clients.append((asyncio.get_running_loop(), queue))
            return list(self._history), queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._clients = [c for c in self._clients if c[1] is not queue]

    def history(self) -> list[dict]:
        with self._lock:
            return list(self._history)


def sse(event: dict) -> str:
    return f"event: message\ndata: {json.dumps(event)}\n\n"


def _error_event(run_id: str, message: str, where: str = "dashboard.replay") -> dict:
    return {"t": time.time(), "run_id": run_id, "type": "error",
            "data": {"where": where, "message": message}}


RunFn = Callable[[TaskSpec, EventBus], object]  # runs one RepairLoop to the end (blocking)


class TaskRunner:
    """Starts one run at a time from `POST /task`, in a worker thread, its events fed to the hub."""

    def __init__(self, run_fn: RunFn, hub: EventHub, runs_dir: Optional[Path]):
        self._run_fn, self._hub, self._runs_dir = run_fn, hub, runs_dir
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, text: str) -> str:
        """Parse and start. ValueError if the text doesn't parse, RuntimeError if a run is going."""
        try:
            task = parse_task(text)
        except ValueError as e:
            self._hub.publish(_error_event("dashboard", str(e), where="dashboard.task"))
            raise
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("a run is already going; wait for it to finish")
            bus = EventBus(new_run_id(), self._runs_dir)
            self._hub.attach(bus)
            self._thread = threading.Thread(target=self._work, args=(task, bus), daemon=True,
                                            name=f"run-{bus.run_id}")
            self._thread.start()
        return bus.run_id

    def join(self, timeout_s: Optional[float] = None) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout_s)

    def _work(self, task: TaskSpec, bus: EventBus) -> None:
        try:
            self._run_fn(task, bus)
        except Exception as e:  # the loop died: say so on the big screen, never fail silently
            bus.emit("error", where="dashboard.task", message=f"{type(e).__name__}: {e}")
        finally:
            bus.close()


def loop_run_fn(cfg, approver: DashboardApprover, env_name: str, author_name: str) -> RunFn:
    """A RunFn wired like `skillsmith run --approver dashboard`: same env, author and loop."""
    from skillsmith.cli import _author, _check_env_factory, _run
    from skillsmith.hal.base import make_env
    from skillsmith.skills.library import SkillLibrary

    def run(task: TaskSpec, bus: EventBus) -> int:
        target = make_env(env_name, cfg)
        make_check_env = _check_env_factory("fake" if env_name == "fake" else "sim", cfg)
        library = SkillLibrary(cfg.path(cfg.repair.library_dir))
        author = _author(author_name, cfg, bus, make_check_env)
        return _run(task, target, make_check_env, library, author, approver, cfg, bus)
    return run


def read_events(path: Path) -> Iterator[dict]:
    """Events from an events.jsonl. A bad line becomes an `error` event, never a silent skip."""
    run_id = path.parent.name
    with open(path) as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                event = _error_event(run_id, f"{path.name} line {n}: bad JSON ({e.msg})")
            yield event


def replay_delays(events: list[dict], speed: float, max_gap_s: float = MAX_REPLAY_GAP_S) -> list[float]:
    """Seconds to wait before each event. speed <= 0 means as fast as possible."""
    delays, prev = [], None
    for e in events:
        t = e.get("t")
        gap = 0.0 if prev is None or t is None else min(max(t - prev, 0.0), max_gap_s)
        delays.append(gap / speed if speed > 0 else 0.0)
        prev = t if t is not None else prev
    return delays


def run_path(runs_dir: Path, run_id: str) -> Optional[Path]:
    """runs/<run_id>/events.jsonl, or None if the id is unsafe or the file is missing."""
    if not _RUN_ID.match(run_id):
        return None
    path = Path(runs_dir) / run_id / "events.jsonl"
    return path if path.is_file() else None


def frame_path(runs_dir: Path, path: str, types: dict[str, str] = IMAGE_TYPES) -> Optional[Path]:
    """A file the events point at (key frames, snapshots, sim videos), only if it lies inside runs_dir."""
    root = Path(runs_dir).resolve()
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else Path.cwd() / candidate).resolve()
    if not resolved.is_relative_to(root):
        resolved = (root / candidate).resolve()  # also accept paths relative to runs_dir
    ok = resolved.is_relative_to(root) and resolved.suffix.lower() in types and resolved.is_file()
    return resolved if ok else None


def open_camera(base_url: str, cam: str, timeout_s: float = CAMERA_TIMEOUT_S):
    """Open robotd's MJPEG stream. Returns (content_type, chunk iterator); OSError if unreachable."""
    import urllib.request

    upstream = urllib.request.urlopen(f"{base_url.rstrip('/')}/stream/{cam}", timeout=timeout_s)
    content_type = upstream.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=frame")

    def chunks() -> Iterator[bytes]:
        try:
            while chunk := upstream.read1(CAMERA_CHUNK):
                yield chunk
        except OSError:  # robotd went away mid-stream; the page shows "camera offline" and retries
            pass
        finally:
            upstream.close()
    return content_type, chunks()


def list_runs(runs_dir: Path) -> list[dict]:
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    out = []
    for d in sorted(runs_dir.iterdir(), reverse=True):
        f = d / "events.jsonl"
        if f.is_file():
            out.append({"run_id": d.name, "events": sum(1 for line in open(f) if line.strip())})
    return out


async def replay_stream(path: Path, speed: float, start: int = 0) -> AsyncIterator[str]:
    """Replay events[start:]; the first one goes out at once, the rest keep their (sped-up) gaps."""
    events = list(read_events(path))
    rest = events[start:]
    for event, delay in zip(rest, replay_delays(rest, speed)):
        if delay > 0:
            await asyncio.sleep(delay)
        yield sse({**event, "replay": True})
    end = {"run_id": path.parent.name, "events": len(events)}
    yield f"event: replay.end\ndata: {json.dumps(end)}\n\n"


async def live_stream(hub: EventHub, request) -> AsyncIterator[str]:
    history, queue = hub.subscribe()
    try:
        for event in history:
            yield sse(event)
        while not await request.is_disconnected():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            yield sse(event)
    finally:
        hub.unsubscribe(queue)


def _hostname(netloc: str) -> str:
    """'127.0.0.1:8080' -> '127.0.0.1', '[::1]:8080' -> '::1' (lower case)."""
    try:
        return (urlsplit(f"//{netloc}").hostname or "").lower()
    except ValueError:  # malformed Host header
        return ""


def host_refusal(headers, allowed_hosts: Optional[frozenset]) -> Optional[str]:
    """Why the Host header is refused (DNS rebinding), or None. No allow-list means no check."""
    if allowed_hosts is None:
        return None
    host = _hostname(headers.get("host", ""))
    return None if host in allowed_hosts else f"host {host or '(none)'!r} is not allowed"


def origin_refusal(headers) -> Optional[str]:
    """Why a state-changing request looks cross-site, or None. A browser always sends Origin
    (or Sec-Fetch-Site) on a cross-site POST; scripts and curl send neither and are allowed."""
    site = headers.get("sec-fetch-site")
    if site in ("cross-site", "same-site"):
        return f"cross-site request (Sec-Fetch-Site: {site})"
    origin = headers.get("origin")
    if origin is None:
        return None
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or parts.netloc.lower() != headers.get("host", "").lower():
        return f"origin {origin!r} is not this dashboard"
    return None


def allowed_hosts_for(bind_host: str, extra: tuple[str, ...] = ()) -> Optional[frozenset]:
    """Host names the dashboard answers to. None (no Host check) when bound to every interface
    without an explicit list: the operator chose to expose it."""
    if bind_host in ("0.0.0.0", "::") and not extra:
        return None
    return frozenset({"127.0.0.1", "localhost", "::1", bind_host.lower(), *(h.lower() for h in extra)})


async def _json_body(request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        body = None
    return body if isinstance(body, dict) else {}


def create_app(runs_dir: Path, hub: Optional[EventHub] = None,
               approver: Optional[DashboardApprover] = None, run_fn: Optional[RunFn] = None,
               camera_url: Optional[str] = None, allowed_hosts: Optional[frozenset] = None):
    """`run_fn` is None when the server only replays (then `POST /task` answers 503).
    `camera_url` is robotd's base URL; None means no live camera (`/camera/*` answers 503).
    `allowed_hosts` (see `allowed_hosts_for`) refuses other Host headers with 403; None skips it."""
    from fastapi import FastAPI, HTTPException, Query, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

    app = FastAPI(title="Skillsmith dashboard")

    def refuse(reason: str, where: str) -> None:
        app.state.hub.publish(_error_event("dashboard", f"refused: {reason}", where=where))
        raise HTTPException(403, reason)

    @app.middleware("http")
    async def check_host(request: Request, call_next):
        reason = host_refusal(request.headers, allowed_hosts)
        if reason is not None:
            app.state.hub.publish(_error_event("dashboard", f"refused: {reason}", where="dashboard.host"))
            return JSONResponse({"detail": reason}, status_code=403)
        return await call_next(request)

    async def guarded_body(request: Request, where: str) -> dict:
        """The JSON body of a state-changing POST from this dashboard's own page (or a script)."""
        reason = origin_refusal(request.headers)
        if reason is not None:
            refuse(reason, where)
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":  # a cross-site form can't send this without a preflight
            raise HTTPException(415, "send JSON (Content-Type: application/json)")
        return await _json_body(request)

    app.state.hub = hub or EventHub()
    app.state.runs_dir = Path(runs_dir)
    app.state.approver = approver or DashboardApprover()
    app.state.runner = TaskRunner(run_fn, app.state.hub, app.state.runs_dir) if run_fn else None
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML.read_text()

    @app.get("/runs")
    def runs() -> list[dict]:
        return list_runs(app.state.runs_dir)

    @app.get("/improvement")
    def improvement() -> list[dict]:
        from skillsmith.dashboard.improvement import improvement as summaries
        return summaries(app.state.runs_dir)

    @app.get("/frame")
    def frame(path: str):
        found = frame_path(app.state.runs_dir, path)
        if found is None:
            raise HTTPException(404, f"no image {path!r} under the runs dir")
        return FileResponse(found, media_type=IMAGE_TYPES[found.suffix.lower()])

    @app.get("/video")
    def video(path: str):
        found = frame_path(app.state.runs_dir, path, VIDEO_TYPES)
        if found is None:
            raise HTTPException(404, f"no video {path!r} under the runs dir")
        return FileResponse(found, media_type=VIDEO_TYPES[found.suffix.lower()])

    @app.get("/camera/{cam}")
    def camera(cam: str):
        if cam not in CAMERAS:
            raise HTTPException(404, f"no camera {cam!r}; one of {', '.join(CAMERAS)}")
        if not camera_url:
            raise HTTPException(503, "no live camera: this dashboard has no robotd URL")
        try:
            content_type, chunks = open_camera(camera_url, cam)
        except (OSError, ValueError) as e:  # URLError is an OSError
            raise HTTPException(503, f"camera {cam} offline: robotd not reachable ({e})") from e
        return StreamingResponse(chunks, media_type=content_type, headers=headers)

    @app.get("/events")
    async def events(request: Request, replay: Optional[str] = None, speed: float = 1.0,
                     start: int = Query(0, alias="from", ge=0)):
        if replay is None:
            gen = live_stream(app.state.hub, request)
        else:
            path = run_path(app.state.runs_dir, replay)
            if path is None:
                app.state.hub.publish(_error_event(replay, f"no recorded run {replay!r}"))
                raise HTTPException(404, f"no recorded run {replay!r}")
            gen = replay_stream(path, speed, start)
        return StreamingResponse(gen, media_type="text/event-stream", headers=headers)

    @app.post("/task", status_code=202)
    async def task(request: Request) -> dict:
        runner = app.state.runner
        if runner is None:
            raise HTTPException(503, "this dashboard only replays; start it with `skillsmith serve`")
        text = str((await guarded_body(request, "dashboard.task")).get("text") or "").strip()
        if not text:
            raise HTTPException(400, "empty task")
        try:
            return {"run_id": runner.start(text)}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/approval")
    def approval() -> Optional[dict]:
        return app.state.approver.pending()

    @app.post("/approve")
    async def approve(request: Request) -> dict:
        body = await guarded_body(request, "dashboard.approve")
        approved, nonce = body.get("approved"), body.get("nonce")
        if not isinstance(approved, bool):
            raise HTTPException(400, '"approved" must be true or false')
        if not isinstance(nonce, str) or not nonce:
            raise HTTPException(400, '"nonce" is required: read it from GET /approval')
        if not app.state.approver.decide(approved, str(body.get("note") or ""), body.get("run_id"), nonce):
            raise HTTPException(409, "nothing is waiting for approval with that run and nonce")
        return {"ok": True}

    return app


def main(cfg) -> int:
    try:
        import uvicorn
    except ImportError:
        print("dashboard needs the extra: uv sync --extra dashboard")
        return 1
    opts = cfg.get("dashboard") or {}
    env_name, author_name = opts.get("env", "fake"), opts.get("author", "droid")
    approver = DashboardApprover(timeout_s=float(opts.get("approval_timeout_s", 600.0)))
    run_fn = loop_run_fn(cfg, approver, env_name, author_name)
    camera_url = opts.get("camera_url", (cfg.get("robot") or {}).get("robotd_url"))
    host = str(opts.get("host", HOST))
    allowed = allowed_hosts_for(host, tuple(str(h) for h in opts.get("allowed_hosts") or ()))
    app = create_app(cfg.path(cfg.repair.runs_dir), approver=approver, run_fn=run_fn,
                     camera_url=camera_url, allowed_hosts=allowed)
    print(f"dashboard on http://{host}:{PORT}  env={env_name} author={author_name} camera={camera_url}"
          "  (replay: /?replay=<run_id>&speed=2)")
    if allowed is None:
        print("warning: bound to every interface with no dashboard.allowed_hosts; anyone on the "
              "network can open it (approvals still need the nonce)")
    try:
        uvicorn.run(app, host=host, port=PORT, log_level="warning")
    except KeyboardInterrupt:
        pass
    return 0
