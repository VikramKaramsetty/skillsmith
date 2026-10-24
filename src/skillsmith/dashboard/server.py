"""Dashboard server. OWNER: WS6 (docs/workstreams/WS6_dashboard.md).

Contract:
- `main(cfg) -> int`: called by `skillsmith serve`; serves on :8080 and blocks until Ctrl-C.
- `GET /`: the single-page UI (one HTML file, no build step).
- `GET /events`: SSE stream of events (docs/INTERFACES.md §7) from the live EventBus.
  `GET /events?replay=<run_id>&speed=2` replays runs/<run_id>/events.jsonl (demo fallback).
- `POST /task {"text": "..."}`: parse the task and start a RepairLoop in a worker thread.
- `POST /approve {"approved": bool, "note": str}`: resolves the pending DashboardApprover request.
- The UI renders ONLY from events. If it needs more data, request a new event field from the lead.

WS6.1 builds the event feed: the live hub, replay and the run list. `/task` and `/approve`
arrive in WS6.3. FastAPI is imported lazily so this module imports with core deps only.
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
from typing import AsyncIterator, Iterator, Optional

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


def _error_event(run_id: str, message: str) -> dict:
    return {"t": time.time(), "run_id": run_id, "type": "error",
            "data": {"where": "dashboard.replay", "message": message}}


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


def create_app(runs_dir: Path, hub: Optional[EventHub] = None):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, StreamingResponse

    app = FastAPI(title="Skillsmith dashboard")
    app.state.hub = hub or EventHub()
    app.state.runs_dir = Path(runs_dir)
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

    return app


def main(cfg) -> int:
    try:
        import uvicorn
    except ImportError:
        print("dashboard needs the extra: uv sync --extra dashboard")
        return 1
    app = create_app(cfg.path(cfg.repair.runs_dir))
    print(f"dashboard on http://0.0.0.0:{PORT}  (replay: /?replay=<run_id>&speed=2)")
    try:
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
    except KeyboardInterrupt:
        pass
    return 0
