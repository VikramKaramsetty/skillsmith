"""WS6.3: DashboardApprover, `POST /approve` and the task input `POST /task`."""

import threading
import time

import pytest

from skillsmith.dashboard.approver import TIMED_OUT, DashboardApprover
from skillsmith.dashboard.server import EventHub, create_app, loop_run_fn

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

TASK = "stack the blue cup on the red cup"
SUMMARY = {"title": "run stack v1 on fake", "skill": "stack", "version": 1}


def _request_in_thread(approver, run_id="r1", summary=SUMMARY):
    out = {}
    t = threading.Thread(target=lambda: out.update(answer=approver.request(run_id, summary)), daemon=True)
    t.start()
    assert approver.wait_pending(2.0) is not None
    return t, out


def _types(app) -> list[str]:
    return [e["type"] for e in app.state.hub.history()]


# ---- the approver on its own ------------------------------------------------------------

def test_request_blocks_until_decided_from_another_thread():
    approver = DashboardApprover(timeout_s=5)
    t, out = _request_in_thread(approver)
    assert approver.pending() == {"run_id": "r1", "kind": "approval", "title": "run stack v1 on fake"}
    assert t.is_alive()
    assert approver.decide(False, "grip lower", run_id="r1")
    t.join(2)
    assert out["answer"] == (False, "grip lower")
    assert approver.pending() is None


def test_timeout_is_a_reject():
    start = time.monotonic()
    assert DashboardApprover(timeout_s=0.1).request("r1", SUMMARY) == (False, TIMED_OUT)
    assert time.monotonic() - start < 2


def test_decide_without_a_request_or_for_another_run_is_refused():
    approver = DashboardApprover(timeout_s=5)
    assert not approver.decide(True)
    t, out = _request_in_thread(approver, summary={"kind": "reset", "title": "Reset the table"})
    assert approver.pending()["kind"] == "reset"
    assert not approver.decide(True, run_id="other-run")
    assert approver.decide(True)
    t.join(2)
    assert out["answer"] == (True, "")


# ---- HTTP -------------------------------------------------------------------------------

def test_approving_over_http_unblocks_a_waiting_approver(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    assert client.get("/approval").json() is None
    t, out = _request_in_thread(app.state.approver)
    assert client.get("/approval").json()["title"] == "run stack v1 on fake"
    assert client.post("/approve", json={"approved": True, "run_id": "r1"}).json() == {"ok": True}
    t.join(2)
    assert out["answer"] == (True, "")


def test_reject_over_http_carries_the_note(tmp_path):
    app = create_app(tmp_path)
    t, out = _request_in_thread(app.state.approver)
    r = TestClient(app).post("/approve", json={"approved": False, "note": "too close to the edge"})
    assert r.status_code == 200
    t.join(2)
    assert out["answer"] == (False, "too close to the edge")


def test_bad_or_unmatched_approve_is_refused(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    assert client.post("/approve", json={"approved": True}).status_code == 409  # nothing waits
    t, out = _request_in_thread(app.state.approver)
    assert client.post("/approve", json={"approved": "yes"}).status_code == 400
    assert client.post("/approve", content=b"not json").status_code == 400
    assert client.post("/approve", json={"approved": True, "run_id": "stale"}).status_code == 409
    assert app.state.approver.pending() is not None  # still waiting for the right answer
    app.state.approver.decide(False, "cleanup")
    t.join(2)


def test_task_needs_a_runner(tmp_path):
    assert TestClient(create_app(tmp_path)).post("/task", json={"text": TASK}).status_code == 503


def test_task_starts_a_run_with_its_events_on_the_hub(tmp_path):
    gate, seen = threading.Event(), []

    def run_fn(task, bus):
        seen.append(task)
        bus.emit("run.started", task=task)
        gate.wait(5)
        bus.emit("run.finished", ok=True, rounds=0)

    app = create_app(tmp_path, run_fn=run_fn)
    client = TestClient(app)
    r = client.post("/task", json={"text": TASK})
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    assert client.post("/task", json={"text": TASK}).status_code == 409  # one run at a time
    gate.set()
    app.state.runner.join(5)
    assert seen[0].skill == "stack" and seen[0].text == TASK
    assert _types(app) == ["run.started", "run.finished"]
    assert {e["run_id"] for e in app.state.hub.history()} == {run_id}
    assert (tmp_path / run_id / "events.jsonl").is_file()
    assert client.post("/task", json={"text": TASK}).status_code == 202  # free again


def test_unparseable_or_empty_task_is_400_and_an_error_event(tmp_path):
    app = create_app(tmp_path, run_fn=lambda task, bus: None)
    client = TestClient(app)
    assert client.post("/task", json={"text": ""}).status_code == 400
    r = client.post("/task", json={"text": "juggle three cups"})
    assert r.status_code == 400 and "can't parse" in r.json()["detail"]
    (err,) = app.state.hub.history()
    assert err["type"] == "error" and err["data"]["where"] == "dashboard.task"


def test_a_crashing_run_emits_an_error_event(tmp_path):
    def run_fn(task, bus):
        raise RuntimeError("robotd unreachable")

    app = create_app(tmp_path, run_fn=run_fn)
    TestClient(app).post("/task", json={"text": TASK})
    app.state.runner.join(5)
    (err,) = app.state.hub.history()
    assert err["type"] == "error" and "robotd unreachable" in err["data"]["message"]


# ---- the whole gate: a real RepairLoop on the fake world, approved over HTTP -------------

def _cfg(tmp_path, library_dir):
    from skillsmith.config import Config, load_config

    data = load_config().raw()
    return Config({**data, "repair": {**data["repair"], "runs_dir": str(tmp_path / "runs"),
                                      "library_dir": str(library_dir)}})


def test_repair_loop_waits_for_the_dashboard_then_passes(tmp_path):
    from skillsmith.demo import demo_sources
    from skillsmith.hal.base import make_env
    from skillsmith.repair.loop import RepairLoop
    from skillsmith.repair.scripted import ScriptedAuthor
    from skillsmith.skills.library import SkillLibrary

    cfg = _cfg(tmp_path, tmp_path / "empty_lib")  # no `stack` yet, so Droid's draft needs approval
    (tmp_path / "empty_lib").mkdir()
    v1, _ = demo_sources()
    approver = DashboardApprover(timeout_s=10)

    def run_fn(task, bus):
        loop = RepairLoop(cfg, make_env("fake", cfg), lambda scene: make_env("fake", cfg, scene),
                          SkillLibrary(tmp_path / "empty_lib"), ScriptedAuthor([v1], []), approver, bus)
        return loop.run(task)

    app = create_app(tmp_path / "runs", hub=EventHub(), approver=approver, run_fn=run_fn)
    client = TestClient(app)
    run_id = client.post("/task", json={"text": TASK}).json()["run_id"]
    pending = approver.wait_pending(10)
    assert pending["run_id"] == run_id
    assert _types(app)[-1] == "approval.requested"  # the loop is parked on the human gate
    assert client.post("/approve", json={"approved": True, "run_id": run_id}).status_code == 200
    app.state.runner.join(10)
    done = app.state.hub.history()[-1]
    assert done["type"] == "run.finished" and done["data"]["ok"] is True


def test_loop_run_fn_runs_a_library_skill_on_fake(tmp_path):
    from skillsmith.demo import temp_library

    cfg = _cfg(tmp_path, temp_library())
    app = create_app(tmp_path / "runs", approver=DashboardApprover(timeout_s=5),
                     run_fn=loop_run_fn(cfg, DashboardApprover(timeout_s=5), "fake", "scripted"))
    task = "move the green block to 0.28, 0.0"  # pick_place is a seed library skill: no approval
    assert TestClient(app).post("/task", json={"text": task}).status_code == 202
    app.state.runner.join(20)
    types = _types(app)
    assert types[0] == "run.started" and types[-1] == "run.finished", app.state.hub.history()[-1]
    assert app.state.hub.history()[-1]["data"]["ok"] is True
