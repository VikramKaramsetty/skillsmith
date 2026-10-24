"""WS6.8: per-approval nonce, same-origin check on /approve and /task, loopback bind and Host check."""

import threading

import pytest

from skillsmith.dashboard import server
from skillsmith.dashboard.approver import DashboardApprover
from skillsmith.dashboard.server import allowed_hosts_for, create_app, host_refusal, origin_refusal

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

TASK = "stack the blue cup on the red cup"
SUMMARY = {"title": "run stack v1 on fake", "skill": "stack", "version": 1}


def _waiting(approver, run_id="r1"):
    out = {}
    t = threading.Thread(target=lambda: out.update(answer=approver.request(run_id, SUMMARY)), daemon=True)
    t.start()
    assert approver.wait_pending(2.0) is not None
    return t, out


# ---- nonce ------------------------------------------------------------------------------

def test_each_request_gets_a_fresh_nonce_and_a_wrong_one_is_refused():
    approver = DashboardApprover(timeout_s=5)
    t, out = _waiting(approver)
    first = approver.pending()["nonce"]
    assert len(first) >= 16
    assert not approver.decide(True, nonce="guess")
    assert approver.decide(True, nonce=first)
    t.join(2)
    assert out["answer"] == (True, "")
    t, out = _waiting(approver)
    second = approver.pending()["nonce"]
    assert second != first
    assert not approver.decide(True, nonce=first)  # a stale nonce never approves the next request
    assert approver.decide(False, "done", nonce=second)
    t.join(2)


def test_http_approve_needs_the_current_nonce(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    t, out = _waiting(app.state.approver)
    assert client.post("/approve", json={"approved": True, "run_id": "r1"}).status_code == 400
    assert client.post("/approve", json={"approved": True, "run_id": "r1", "nonce": "x"}).status_code == 409
    assert app.state.approver.pending() is not None  # still waiting
    nonce = client.get("/approval").json()["nonce"]
    assert client.post("/approve", json={"approved": True, "run_id": "r1", "nonce": nonce}).status_code == 200
    t.join(2)
    assert out["answer"] == (True, "")


def test_nonce_is_never_in_an_event(tmp_path):
    from skillsmith.events import EventBus

    approver = DashboardApprover(timeout_s=5)
    app = create_app(tmp_path, approver=approver)
    bus = EventBus("r1", tmp_path)
    app.state.hub.attach(bus)
    bus.emit("approval.requested", summary=SUMMARY)
    t, _ = _waiting(approver)
    nonce = approver.pending()["nonce"]
    approver.decide(True, nonce=nonce)
    t.join(2)
    bus.close()
    assert nonce not in str(app.state.hub.history())
    assert nonce not in (tmp_path / "r1" / "events.jsonl").read_text()


# ---- same origin ------------------------------------------------------------------------

def test_origin_refusal_rules():
    host = {"host": "127.0.0.1:8080"}
    assert origin_refusal(host) is None  # curl, scripts, TestClient
    assert origin_refusal({**host, "origin": "http://127.0.0.1:8080"}) is None
    assert origin_refusal({**host, "sec-fetch-site": "same-origin"}) is None
    assert origin_refusal({**host, "origin": "http://evil.example"})
    assert origin_refusal({**host, "origin": "http://127.0.0.1:9999"})
    assert origin_refusal({**host, "origin": "null"})
    assert origin_refusal({**host, "sec-fetch-site": "cross-site"})
    assert origin_refusal({**host, "sec-fetch-site": "same-site"})


def test_cross_site_approve_is_403_and_an_error_event(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    t, out = _waiting(app.state.approver)
    nonce = app.state.approver.pending()["nonce"]
    body = {"approved": True, "run_id": "r1", "nonce": nonce}
    assert client.post("/approve", json=body, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/approve", json=body, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert app.state.approver.pending() is not None
    errors = [e for e in app.state.hub.history() if e["type"] == "error"]
    assert len(errors) == 2 and all(e["data"]["where"] == "dashboard.approve" for e in errors)
    assert client.post("/approve", json=body, headers={"Origin": "http://testserver"}).status_code == 200
    t.join(2)
    assert out["answer"] == (True, "")


def test_approve_and_task_need_json_content_type(tmp_path):
    app = create_app(tmp_path, run_fn=lambda task, bus: None)
    client = TestClient(app)
    form = {"Content-Type": "text/plain"}  # what a cross-site <form enctype=text/plain> sends
    assert client.post("/approve", content=b'{"approved": true}', headers=form).status_code == 415
    assert client.post("/task", content=b'{"text": "x"}', headers=form).status_code == 415


def test_cross_site_task_is_refused(tmp_path):
    started = []
    app = create_app(tmp_path, run_fn=lambda task, bus: started.append(task))
    client = TestClient(app)
    r = client.post("/task", json={"text": TASK}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert not app.state.runner.busy() and not started
    (err,) = app.state.hub.history()
    assert err["type"] == "error" and err["data"]["where"] == "dashboard.task"
    assert client.post("/task", json={"text": TASK}, headers={"Origin": "http://testserver"}).status_code == 202
    app.state.runner.join(5)


# ---- bind and Host ----------------------------------------------------------------------

def test_binds_loopback_by_default():
    assert server.HOST == "127.0.0.1"


def test_allowed_hosts():
    loop = allowed_hosts_for("127.0.0.1")
    assert {"127.0.0.1", "localhost", "::1"} <= loop and "evil.example" not in loop
    assert allowed_hosts_for("0.0.0.0") is None  # explicit wide bind: no Host check
    assert "gx10.local" in allowed_hosts_for("0.0.0.0", ("gx10.local",))
    assert host_refusal({"host": "localhost:8080"}, loop) is None
    assert host_refusal({"host": "[::1]:8080"}, loop) is None
    assert host_refusal({"host": "rebind.evil.example:8080"}, loop)
    assert host_refusal({"host": "[bad"}, loop)
    assert host_refusal({"host": "evil"}, None) is None


def test_foreign_host_header_is_refused_dns_rebinding(tmp_path):
    app = create_app(tmp_path, allowed_hosts=allowed_hosts_for("127.0.0.1"))
    assert TestClient(app, base_url="http://127.0.0.1:8080").get("/approval").status_code == 200
    assert TestClient(app, base_url="http://localhost:8080").get("/runs").status_code == 200
    r = TestClient(app, base_url="http://rebind.evil.example:8080").get("/approval")
    assert r.status_code == 403
    (err,) = app.state.hub.history()
    assert err["type"] == "error" and err["data"]["where"] == "dashboard.host"


def test_page_sends_the_nonce():
    assert "nonce: pending.nonce" in server.INDEX_HTML.read_text()
