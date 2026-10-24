"""WS6.9: improvement rows tag env/author; environment approvals say "Fixed, retry"; the page
treats any event flagged "replay": true as a recording."""

from test_improvement import REUSE, _js, live_summary
from test_story_view import DEMO, FINISH, _events, fold

from skillsmith.dashboard.improvement import run_summary
from skillsmith.dashboard.server import INDEX_HTML

# A drafted skill: static gate, sim check run, approval, then the real arm. No Droid session.
DRAFT = [
    ("run.started", {"task": {"text": "put the cup in the bowl", "skill": "put_in"}}),
    ("plan.selected", {"skill": "put_in", "source": "draft", "version": 0}),
    ("skill.diff", {"old_version": None, "new_version": 0, "diff": "+x"}),
    ("gate.result", {"gate": "static", "ok": True, "detail": "ok"}),
    ("attempt.started", {"env": "sim", "skill": "put_in", "version": 0}),
    ("attempt.finished", {"env": "sim", "skill": "put_in", "version": 0, "ok": True, "error": None}),
    ("gate.result", {"gate": "sim", "ok": True, "detail": "task achieved in sim"}),
    ("gate.result", {"gate": "safety", "ok": True, "detail": "ok"}),
    ("approval.requested", {"summary": {"title": "run put_in v0 on real"}}),
    ("approval.decided", {"approved": True, "note": ""}),
    ("attempt.started", {"env": "real", "skill": "put_in", "version": 0}),
    ("attempt.finished", {"env": "real", "skill": "put_in", "version": 0, "ok": True, "error": None}),
    ("skill.saved", {"skill": "put_in", "version": 1}),
    ("run.finished", {"ok": True, "rounds": 0}),
]
# The demo repaired by the `droid exec` CLI fallback: its droid.started has `cmd`, no `via`.
CLI = DEMO[:6] + [("droid.started", {"round": 1, "cmd": "droid exec --output-format stream-json"})] + DEMO[7:]
ENV_APPROVAL = ("approval.requested", {"summary": {
    "kind": "environment", "title": "CAMERA_BUMPED: not a code problem. Fix the table or camera, then approve to retry",
    "class": "CAMERA_BUMPED", "phase": None, "summary": "marker drift 2.1 cm", "hint": "re-seat the camera",
    "skill": "stack", "version": 1, "env": "real"}})


def _tags(pairs):
    s = run_summary(_events(pairs))
    return s["env"], s["author"]


def test_python_tags_env_and_author():
    assert _tags(DEMO + FINISH) == ("real", "droid-sdk")
    assert _tags(REUSE) == ("real", None)  # nothing written: no author
    assert _tags(DRAFT) == ("real", "scripted")  # the sim gate's check run is not the env
    assert _tags(DRAFT[:8] + [("run.finished", {"ok": False, "rounds": 0})]) == (None, "scripted")
    assert _tags(CLI + FINISH) == ("real", "droid-cli")


def test_page_tags_match_python(tmp_path):
    for pairs in [DEMO + FINISH, REUSE, DRAFT, DRAFT[:8], CLI]:
        events = _events(pairs)
        assert live_summary(events, tmp_path) == run_summary(events)


def test_row_tag(tmp_path):
    rows = [{"env": "real", "author": "droid-sdk"}, {"env": "fake", "author": "scripted"},
            {"env": "real", "author": None}, {"env": None, "author": None}]
    assert _js("a.map(m.runTag)", rows, tmp_path) == ["real · droid-sdk", "fake · scripted", "real", "–"]


def test_demo_fake_run_is_tagged_fake_and_scripted(tmp_path):
    from argparse import Namespace

    from skillsmith.cli import cmd_demo_fake
    from skillsmith.config import Config, load_config
    from skillsmith.dashboard.improvement import improvement

    data = load_config().raw()
    cfg = Config({**data, "repair": {**data["repair"], "runs_dir": str(tmp_path / "runs")}})
    assert cmd_demo_fake(Namespace(author="scripted"), cfg) == 0
    (s,) = improvement(tmp_path / "runs")
    assert (s["env"], s["author"]) == ("fake", "scripted")


def test_environment_approval_says_fixed_retry(tmp_path):
    s = fold(_events(DEMO[:4] + [ENV_APPROVAL]), tmp_path)
    assert s["approval"]["kind"] == "environment" and s["approval"]["state"] == "pending"
    assert s["banner"]["kind"] == "wait" and "CAMERA_BUMPED" in s["banner"]["text"]
    assert s["banner"]["sub"] == "re-seat the camera"
    labels = _js("['environment', 'reset', 'approval', null].map(m.approvalLabels)", [], tmp_path)
    assert labels[0]["approve"] == "Fixed, retry" and labels[0]["reject"] == "Stop"
    assert "Droid is not called" in labels[0]["note"]
    assert labels[1]["approve"] == "Done" and labels[1]["reject"] is None
    assert labels[2] == labels[3] and labels[2]["approve"] == "Approve"


def test_environment_decisions(tmp_path):
    fixed = fold(_events(DEMO[:4] + [ENV_APPROVAL, ("approval.decided", {"approved": True, "note": ""})]), tmp_path)
    assert fixed["approval"]["state"] == "approved" and fixed["banner"]["text"] == "FIXED · RETRYING"
    stop = fold(_events(DEMO[:4] + [ENV_APPROVAL, ("approval.decided", {"approved": False, "note": "no"})]), tmp_path)
    assert stop["approval"]["state"] == "rejected" and stop["banner"]["text"] == "STOPPED · not fixed"


def test_code_approval_unchanged(tmp_path):
    s = fold(_events(DEMO), tmp_path)
    assert s["approval"]["kind"] == "approval" and s["banner"]["text"] == "WAITING FOR APPROVAL"


def test_replay_flag_marks_a_recording(tmp_path):
    events = _events(DEMO[:3])
    flagged = [{**e, "replay": True} for e in events]
    runs = [events, flagged, flagged[:1] + events[1:], flagged + events[:1]]
    got = _js("a.map((evs) => evs.reduce(m.reduceRecorded, false))", runs, tmp_path)
    assert got == [False, True, True, False]  # a later live run.started ends the recording


def test_page_gates_controls_on_the_flag_not_only_the_url():
    html = INDEX_HTML.read_text()
    assert "reduceRecorded(recorded, e)" in html
    assert '!!(a && a.state === "pending") && !recorded' in html
    assert '$("task-run").disabled = recorded' in html
