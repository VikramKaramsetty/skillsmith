"""WS6.1: dashboard server, SSE live feed and replay of a recorded events.jsonl."""

import asyncio
import json
import threading
import time

import pytest

from skillsmith.dashboard import server
from skillsmith.dashboard.server import EventHub, create_app, live_stream, replay_delays
from skillsmith.events import EventBus

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

TYPES = ["run.started", "attempt.started", "attempt.classified", "droid.started", "skill.diff",
         "gate.result", "approval.requested", "run.finished"]


@pytest.fixture
def runs_dir(tmp_path):
    bus = EventBus("20261024-120000-ab12", tmp_path)
    for i, type_ in enumerate(TYPES):
        bus.emit(type_, index=i)
    bus.close()
    return tmp_path


def _sse_messages(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if not line.startswith(":"))
        out.append((fields["event"], json.loads(fields["data"])))
    return out


def _replay(client, run_id, speed=0):
    with client.stream("GET", f"/events?replay={run_id}&speed={speed}") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        return _sse_messages(r.read().decode())


def test_serves_the_page(runs_dir):
    r = TestClient(create_app(runs_dir)).get("/")
    assert r.status_code == 200
    assert "EventSource" in r.text


def test_replay_streams_recorded_file_in_order(runs_dir):
    msgs = _replay(TestClient(create_app(runs_dir)), "20261024-120000-ab12")
    *events, (end_name, end) = msgs
    assert [e["type"] for _, e in events] == TYPES
    assert [e["data"]["index"] for _, e in events] == list(range(len(TYPES)))
    assert all(name == "message" and e["replay"] is True for name, e in events)
    assert end_name == "replay.end" and end["events"] == len(TYPES)


def test_replay_unknown_or_unsafe_run_is_404_and_emits_error(runs_dir):
    app = create_app(runs_dir)
    client = TestClient(app)
    assert client.get("/events?replay=nope").status_code == 404
    assert client.get("/events?replay=..%2F..%2Fetc").status_code == 404
    assert [e["type"] for e in app.state.hub.history()] == ["error", "error"]


def test_replay_bad_line_becomes_error_event(runs_dir):
    path = runs_dir / "20261024-120000-ab12" / "events.jsonl"
    path.write_text(path.read_text() + "{not json\n")
    *events, _ = _replay(TestClient(create_app(runs_dir)), "20261024-120000-ab12")
    assert events[-1][1]["type"] == "error"
    assert "bad JSON" in events[-1][1]["data"]["message"]


def test_runs_lists_recordings(runs_dir):
    assert TestClient(create_app(runs_dir)).get("/runs").json() == [
        {"run_id": "20261024-120000-ab12", "events": len(TYPES)}]


def test_replay_delays_follow_speed_and_cap_gaps():
    events = [{"t": 100.0}, {"t": 101.0}, {"t": 103.0}, {"t": 200.0}]
    assert replay_delays(events, speed=1) == [0.0, 1.0, 2.0, server.MAX_REPLAY_GAP_S]
    assert replay_delays(events, speed=2) == [0.0, 0.5, 1.0, server.MAX_REPLAY_GAP_S / 2]
    assert replay_delays(events, speed=0) == [0.0] * 4


def test_replay_speed_changes_wall_time(tmp_path):
    bus = EventBus("slow", tmp_path)
    for i in range(3):
        bus.emit("note", text=str(i))
    bus.close()
    path = tmp_path / "slow" / "events.jsonl"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    for i, e in enumerate(lines):
        e["t"] = 1000.0 + 0.2 * i  # 0.4 s of recorded time
    path.write_text("".join(json.dumps(e) + "\n" for e in lines))
    client = TestClient(create_app(tmp_path))

    def timed(speed):
        start = time.monotonic()
        _replay(client, "slow", speed)
        return time.monotonic() - start

    assert timed(1) >= 0.35
    assert timed(10) < 0.3


class _Request:
    def __init__(self, polls: int):
        self.polls = polls

    async def is_disconnected(self) -> bool:
        self.polls -= 1
        return self.polls < 0


def test_live_stream_sends_history_then_events_from_a_worker_thread(monkeypatch):
    monkeypatch.setattr(server, "HEARTBEAT_S", 0.05)
    hub = EventHub()
    bus = EventBus("live")
    hub.attach(bus)
    bus.emit("run.started", task="stack")

    async def collect():
        out = []
        gen = live_stream(hub, _Request(polls=3))
        out.append(await gen.__anext__())  # history replayed to a late joiner
        threading.Thread(target=bus.emit, args=("attempt.started",), kwargs={"env": "fake"}).start()
        async for chunk in gen:
            out.append(chunk)
        return out

    chunks = asyncio.run(collect())
    types = [json.loads(c.split("data: ", 1)[1])["type"] for c in chunks if c.startswith("event:")]
    assert types == ["run.started", "attempt.started"]
    assert hub._clients == []  # disconnect unsubscribes


def test_main_without_uvicorn_does_not_crash(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_uvicorn(name, *a, **k):
        if name == "uvicorn":
            raise ImportError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_uvicorn)
    assert server.main(cfg=None) == 1
