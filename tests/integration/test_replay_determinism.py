"""QA.3: a replayed run shows the dashboard exactly what the live run showed.

The demo fallback is `/?replay=<run_id>`: the server streams runs/<id>/events.jsonl over the same
SSE feed the live page reads. If the recorded file and the live hub ever disagree (a field lost
in JSON, an event that goes to subscribers but not to the file, an order change, timing leaking
into the story), the fallback would tell a different story from the run it claims to be.

The run goes through the real dashboard path: `POST /task` -> TaskRunner -> EventHub (live) and
EventBus -> events.jsonl (recorded), on the fake env with the scripted author (the demo-fake
story: draft fails on the cup rim, repair passes). Both sides are read back as SSE, parsed with
a strict JSON parser (the browser's JSON.parse rejects NaN/Infinity), and folded through the
page's own reducer from tests/ws6 under node.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest
from qa_extras import require

from skillsmith.config import Config, load_config

require("fastapi", "dashboard")
require("httpx", "dashboard")
from fastapi.testclient import TestClient  # noqa: E402

from skillsmith.dashboard import server  # noqa: E402
from skillsmith.dashboard.server import EventHub, create_app, live_stream  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
TASK = "stack the blue cup on the red cup"

sys.path.insert(0, str(REPO / "tests" / "ws6"))
try:
    from test_story_view import fold  # the page's pure reducer, run under node
finally:
    sys.path.remove(str(REPO / "tests" / "ws6"))


def _cfg(tmp_path: Path) -> Config:
    data = copy.deepcopy(load_config().raw())
    data["repair"]["runs_dir"] = str(tmp_path / "runs")
    data["repair"]["droid_ws_root"] = str(tmp_path / "droid_ws")
    data["repair"]["lessons_file"] = str(tmp_path / "lessons.md")
    return Config(data)


def _demo_run_fn(cfg: Config):
    """The demo-fake loop (`cmd_demo_fake`) as a dashboard RunFn, so the hub sees it live."""
    from skillsmith.cli import _approver, _author, _check_env_factory, _run
    from skillsmith.demo import demo_sources, temp_library
    from skillsmith.hal.base import Env
    from skillsmith.hal.fake import FakeWorld
    from skillsmith.skills.library import SkillLibrary

    def run(task, bus) -> int:
        v1, v2 = demo_sources()
        world = FakeWorld.demo(cfg, rim_slip=True)
        make_check_env = _check_env_factory("fake", cfg)
        author = _author("scripted", cfg, bus, make_check_env, drafts=[v1], repairs=[v2])
        return _run(task, Env("fake", world, world, world), make_check_env, SkillLibrary(temp_library()),
                    author, _approver("auto", "fake"), cfg, bus)
    return run


# ---- SSE helpers --------------------------------------------------------------------------

def _strict_loads(text: str):
    def reject(const: str):
        raise ValueError(f"{const} is not JSON; the browser's JSON.parse drops this event")
    return json.loads(text, parse_constant=reject)


def _sse_messages(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = [line for line in block.splitlines() if not line.startswith(":")]
        if not lines:
            continue  # heartbeat only
        fields = dict(line.split(": ", 1) for line in lines)
        out.append((fields["event"], _strict_loads(fields["data"])))
    return out


class _Request:
    """Stands in for a Starlette request: disconnects after the history is sent."""

    async def is_disconnected(self) -> bool:
        return True


def _live_sse(hub: EventHub) -> list[dict]:
    """What a page connecting to the live feed now receives (the hub's history, as SSE)."""
    async def collect() -> str:
        return "".join([chunk async for chunk in live_stream(hub, _Request())])
    msgs = _sse_messages(asyncio.run(collect()))
    assert {name for name, _ in msgs} == {"message"}
    return [e for _, e in msgs]


def _replay_sse(client: TestClient, run_id: str, speed: float = 0) -> tuple[list[dict], dict]:
    with client.stream("GET", f"/events?replay={run_id}&speed={speed}") as r:
        assert r.status_code == 200
        *msgs, (end_name, end) = _sse_messages(r.read().decode())
    assert end_name == "replay.end"
    assert {name for name, _ in msgs} == {"message"}
    return [e for _, e in msgs], end


def _unflag(events: list[dict]) -> list[dict]:
    assert all(e.get("replay") is True for e in events), "every replayed event must carry replay: true"
    return [{k: v for k, v in e.items() if k != "replay"} for e in events]


# ---- one live run, recorded ---------------------------------------------------------------

@pytest.fixture
def recorded(tmp_path):
    """(client, run_id, live events) after one demo run started from the dashboard."""
    cfg = _cfg(tmp_path)
    app = create_app(tmp_path / "runs", run_fn=_demo_run_fn(cfg))
    client = TestClient(app)
    r = client.post("/task", json={"text": TASK})
    assert r.status_code == 202, r.text
    app.state.runner.join(timeout_s=60)
    assert not app.state.runner.busy(), "demo run did not finish"
    live = _live_sse(app.state.hub)
    assert live and live[-1]["type"] == "run.finished" and live[-1]["data"]["ok"] is True, \
        [e["type"] for e in live]
    return client, r.json()["run_id"], live


def test_replay_is_the_live_stream_event_for_event(recorded):
    client, run_id, live = recorded
    replayed, end = _replay_sse(client, run_id)
    assert end == {"run_id": run_id, "events": len(live)}
    assert [e["type"] for e in _unflag(replayed)] == [e["type"] for e in live]
    for n, (got, want) in enumerate(zip(_unflag(replayed), live)):
        assert got == want, f"event #{n} ({want['type']}) differs between live and replay"


def test_replay_folds_to_the_same_story(recorded, tmp_path):
    client, run_id, live = recorded
    replayed, _ = _replay_sse(client, run_id)
    live_state = fold(live, tmp_path)
    assert live_state["banner"]["kind"] == "pass" and live_state["run_id"] == run_id
    assert fold(replayed, tmp_path) == live_state


def test_every_prefix_of_the_replay_shows_what_live_showed(recorded, tmp_path):
    """The big screen mid-replay matches the big screen at the same moment of the live run."""
    client, run_id, live = recorded
    replayed = _unflag(_replay_sse(client, run_id)[0])
    cuts = sorted({i + 1 for i, e in enumerate(live) if not e["type"].startswith("primitive.")})
    for cut in cuts:
        assert fold(replayed[:cut], tmp_path) == fold(live[:cut], tmp_path), \
            f"story differs after event #{cut - 1} ({live[cut - 1]['type']})"


def test_replay_is_repeatable_and_speed_only_changes_timing(recorded, monkeypatch):
    client, run_id, _ = recorded
    monkeypatch.setattr(server, "MAX_REPLAY_GAP_S", 0.01)  # keep the speed=1 replay quick
    first, _ = _replay_sse(client, run_id, speed=0)
    assert _replay_sse(client, run_id, speed=0)[0] == first
    assert _replay_sse(client, run_id, speed=1)[0] == first
    assert _replay_sse(client, run_id, speed=50)[0] == first


def test_replay_does_not_touch_the_live_feed(recorded):
    client, run_id, live = recorded
    hub = client.app.state.hub
    _replay_sse(client, run_id)
    assert _live_sse(hub) == live, "a replay must not publish into the live hub"


def test_two_runs_of_the_same_task_tell_the_same_story(recorded, tmp_path):
    """Nothing but ids and timestamps varies between runs, so a recording stands for any run."""
    client, run_id, live = recorded
    r = client.post("/task", json={"text": TASK})
    assert r.status_code == 202, r.text
    client.app.state.runner.join(timeout_s=60)
    second_id = r.json()["run_id"]
    assert second_id != run_id
    second, _ = _replay_sse(client, second_id)
    assert [e["type"] for e in second] == [e["type"] for e in live]
    assert _untimed({**fold(second, tmp_path), "run_id": run_id}) == _untimed(fold(live, tmp_path))


def _untimed(value):
    """The story state without timestamps (e.g. the world overlay keeps the scene's `t`)."""
    if isinstance(value, dict):
        return {k: _untimed(v) for k, v in value.items() if k != "t"}
    if isinstance(value, list):
        return [_untimed(v) for v in value]
    return value


def test_strict_parser_rejects_what_the_browser_rejects():
    assert _sse_messages(server.sse({"t": 1.0, "type": "note", "data": {"x": 1.5}}))
    with pytest.raises(ValueError, match="NaN"):
        _sse_messages(server.sse({"t": 1.0, "type": "note", "data": {"x": float("nan")}}))
