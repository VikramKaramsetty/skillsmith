"""WS6.7: replay mode with speed control (pause, resume, speed change mid-run, restart)."""

import json
import re
import shutil
import subprocess
import time

import pytest

from skillsmith.dashboard.server import INDEX_HTML, create_app
from skillsmith.events import EventBus

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

RUN = "20261024-130000-cd34"
N = 6
NODE = shutil.which("node")


def _sse_messages(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if not line.startswith(":"))
        out.append((fields["event"], json.loads(fields["data"])))
    return out


def _reducer_js() -> str:
    m = re.search(r'^<script id="story-reducer">(.*?)</script>', INDEX_HTML.read_text(), re.S | re.M)
    assert m
    return m.group(1)


@pytest.fixture
def client(tmp_path):
    bus = EventBus(RUN, tmp_path)
    for i in range(N):
        bus.emit("note", index=i)
    bus.close()
    path = tmp_path / RUN / "events.jsonl"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    for i, e in enumerate(lines):
        e["t"] = 1000.0 + 0.1 * i  # 0.5 s of recorded time
    path.write_text("".join(json.dumps(e) + "\n" for e in lines))
    return TestClient(create_app(tmp_path))


def _get(client, query: str):
    with client.stream("GET", f"/events?replay={RUN}&{query}") as r:
        return r.status_code, (_sse_messages(r.read().decode()) if r.status_code == 200 else [])


def test_resumed_events_are_the_same_events_as_a_full_replay(client):
    """The wire format does not change with from= (QA.3 pins it), so a resume is just a suffix."""
    _, full = _get(client, "speed=0")
    _, tail = _get(client, "speed=0&from=2")
    assert tail == full[2:]


def test_from_resumes_at_the_next_event(client):
    _, msgs = _get(client, "speed=0&from=4")
    *events, (name, end) = msgs
    assert [e["data"]["index"] for _, e in events] == [4, 5]
    assert name == "replay.end" and end == {"run_id": RUN, "events": N}  # the total, for the counter


def test_pause_then_resume_at_another_speed_tells_the_whole_story_once(client):
    """What the page does on a speed change: close at event k, reopen with from=k at the new speed."""
    _, first = _get(client, "speed=1")
    k = 3
    seen = [e for _, e in first[:k]]
    start = time.monotonic()
    _, rest = _get(client, f"speed=0&from={k}")
    assert time.monotonic() - start < 0.3  # max speed: no recorded gaps waited out
    seen += [e for name, e in rest if name == "message"]
    assert [e["data"]["index"] for e in seen] == list(range(N))


def test_resume_does_not_wait_before_its_first_event(client):
    start = time.monotonic()
    _, msgs = _get(client, f"speed=1&from={N - 1}")
    assert time.monotonic() - start < 0.08  # the gap before event 5 was already "played"
    assert [e["data"]["index"] for name, e in msgs if name == "message"] == [N - 1]


def test_from_past_the_end_just_ends(client):
    _, msgs = _get(client, "speed=0&from=99")
    assert [name for name, _ in msgs] == ["replay.end"]


def test_negative_from_is_rejected(client):
    assert _get(client, "speed=0&from=-1")[0] == 422


def test_page_has_replay_controls():
    html = INDEX_HTML.read_text()
    for id_ in ["replay-bar", "replay-play", "replay-speeds", "replay-restart", "replay-pos", "replay-pick"]:
        assert f'id="{id_}"' in html


def _js(expr: str, tmp_path):
    if NODE is None:
        pytest.skip("node not installed")
    lib = tmp_path / "story.js"
    lib.write_text(_reducer_js())
    script = f"const m = require({json.dumps(str(lib))}); process.stdout.write(JSON.stringify({expr}));"
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_replay_url_and_speed_helpers(tmp_path):
    got = _js("[m.replayUrl('r1', 2, 0), m.replayUrl('a b', 0, 7), m.SPEEDS.map(m.speedLabel),"
              " ['4', 'x', '-1', '0'].map(m.parseSpeed)]", tmp_path)
    assert got[0] == "/events?replay=r1&speed=2"
    assert got[1] == "/events?replay=a+b&speed=0&from=7"
    assert got[2] == ["0.5×", "1×", "2×", "4×", "8×", "max"]
    assert got[3] == [4, 1, 1, 0]


def test_replay_url_the_page_builds_is_served(client, tmp_path):
    url = _js(f"m.replayUrl({json.dumps(RUN)}, 0, 2)", tmp_path)
    with client.stream("GET", url) as r:
        msgs = _sse_messages(r.read().decode())
    assert [e["data"]["index"] for name, e in msgs if name == "message"] == list(range(2, N))
