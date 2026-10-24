"""Dashboard server. OWNER: WS6 (docs/workstreams/WS6_dashboard.md).

Contract:
- `main(cfg) -> int`: called by `skillsmith serve`; serves on :8080 and blocks until Ctrl-C.
- `GET /`: the single-page UI (one HTML file, no build step).
- `GET /events`: SSE stream of events (docs/INTERFACES.md §7) from the live EventBus.
  `GET /events?replay=<run_id>&speed=2` replays runs/<run_id>/events.jsonl (demo fallback).
- `POST /task {"text": "..."}`: parse the task and start a RepairLoop in a worker thread.
- `POST /approve {"approved": bool, "note": str}`: resolves the pending DashboardApprover request.
- The UI renders ONLY from events. If it needs more data, request a new event field from the lead.

WS6.1 builds the event feed: the live hub, replay and the run list. WS6.3 adds `/task` (one run
at a time; 400 if the text doesn't parse, 409 while a run is going) and `/approve` (409 if
nothing waits; an optional `run_id` must match the waiting run so a stale page can't approve).
`GET /approval` shows what waits. FastAPI is imported lazily so this module imports with core
deps only.
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

from skillsmith.dashboard.approver import DashboardApprover
from skillsmith.events import EventBus, new_run_id
from skillsmith.tasks import parse_task
from skillsmith.types import TaskSpec

PORT = 8080
MAX_REPLAY_GAP_S = 3.0  # long idle gaps in a recording are shortened to this (before speed)
HEARTBEAT_S = 15.0
HISTORY = 2000  # live events kept so a page opened mid-run still sees the whole story
INDEX_HTML = Path(__file__).with_name("index.html")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


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


async def replay_stream(path: Path, speed: float) -> AsyncIterator[str]:
    events = list(read_events(path))
    for event, delay in zip(events, replay_delays(events, speed)):
        if delay > 0:
            await asyncio.sleep(delay)
        yield sse({**event, "replay": True})
    yield f"event: replay.end\ndata: {json.dumps({'run_id': path.parent.name, 'events': len(events)})}\n\n"


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


async def _json_body(request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        body = None
    return body if isinstance(body, dict) else {}


def create_app(runs_dir: Path, hub: Optional[EventHub] = None,
               approver: Optional[DashboardApprover] = None, run_fn: Optional[RunFn] = None):
    """`run_fn` is None when the server only replays (then `POST /task` answers 503)."""
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, StreamingResponse

    app = FastAPI(title="Skillsmith dashboard")
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

    @app.get("/events")
    async def events(request: Request, replay: Optional[str] = None, speed: float = 1.0):
        if replay is None:
            gen = live_stream(app.state.hub, request)
        else:
            path = run_path(app.state.runs_dir, replay)
            if path is None:
                app.state.hub.publish(_error_event(replay, f"no recorded run {replay!r}"))
                raise HTTPException(404, f"no recorded run {replay!r}")
            gen = replay_stream(path, speed)
        return StreamingResponse(gen, media_type="text/event-stream", headers=headers)

    @app.post("/task", status_code=202)
    async def task(request: Request) -> dict:
        runner = app.state.runner
        if runner is None:
            raise HTTPException(503, "this dashboard only replays; start it with `skillsmith serve`")
        text = str((await _json_body(request)).get("text") or "").strip()
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
        body = await _json_body(request)
        approved = body.get("approved")
        if not isinstance(approved, bool):
            raise HTTPException(400, '"approved" must be true or false')
        if not app.state.approver.decide(approved, str(body.get("note") or ""), body.get("run_id")):
            raise HTTPException(409, "nothing is waiting for approval in that run")
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
    app = create_app(cfg.path(cfg.repair.runs_dir), approver=approver, run_fn=run_fn)
    print(f"dashboard on http://0.0.0.0:{PORT}  env={env_name} author={author_name}"
          "  (replay: /?replay=<run_id>&speed=2)")
    try:
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
    except KeyboardInterrupt:
        pass
    return 0
