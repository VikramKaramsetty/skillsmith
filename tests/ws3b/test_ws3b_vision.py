"""WS3.10 advisory vision second opinion: no network, no key; a fake client stands in for the API."""

import base64
import io
import json
import urllib.error

import pytest

from skillsmith.events import EventBus
from skillsmith.perception import vision
from skillsmith.perception.vision import (
    AnthropicVisionClient,
    VisionOpinion,
    build_prompt,
    client_from_env,
    encode_image,
    parse_reply,
    second_opinion,
)

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"
PNG = b"\x89PNG\r\n\x1a\nfake-png"


class FakeClient:
    model = "fake-vision"

    def __init__(self, reply="", exc=None):
        self.reply, self.exc, self.calls = reply, exc, []

    def ask(self, images, prompt):
        self.calls.append((images, prompt))
        if self.exc:
            raise self.exc
        return self.reply


def recording_bus():
    bus = EventBus("test")
    events = []
    bus.subscribe(events.append)
    return bus, events


# ---------------------------------------------------------------- pure parts


def test_parse_reply_reads_json_even_with_prose_around_it():
    assert parse_reply('{"ok": true, "reason": "Blue cup sits on the red cup."}') == (True, "Blue cup sits on the red cup.")
    ok, reason = parse_reply('Sure:\n```json\n{"ok": false, "reason": "The blue cup is on the table."}\n```')
    assert (ok, reason) == (False, "The blue cup is on the table.")


@pytest.mark.parametrize("text", ["", "I think it worked", '{"ok": "yes"}', "{not json}"])
def test_parse_reply_without_a_clear_verdict_gives_none(text):
    ok, reason = parse_reply(text)
    assert ok is None and reason


def test_prompt_names_goal_and_objects_but_not_geometry():
    p = build_prompt("blue cup on top of red cup", ["blue cup", "red cup"])
    assert "blue cup on top of red cup" in p and "red cup" in p and "JSON" in p
    assert "geometry" not in p.lower()


def test_encode_image_passes_bytes_through():
    assert encode_image(JPEG) == ("image/jpeg", JPEG)
    assert encode_image(PNG) == ("image/png", PNG)


def test_opinion_flags():
    assert VisionOpinion(False, "x", geometry_ok=True).disputes_pass
    assert not VisionOpinion(False, "x", geometry_ok=False).disputes_pass
    assert not VisionOpinion(None, "x", geometry_ok=True).disputes_pass
    assert VisionOpinion(True, "x", geometry_ok=True).agrees is True
    assert VisionOpinion(True, "x").agrees is None
    d = VisionOpinion(False, "x", geometry_ok=True).to_dict()
    assert d["advisory"] is True and d["disputes_pass"] is True and d["agrees"] is False


# ---------------------------------------------------------------- keys from env only


def test_client_from_env_needs_a_key():
    assert client_from_env({}) is None
    assert client_from_env({"ANTHROPIC_API_KEY": "  "}) is None
    c = client_from_env({"ANTHROPIC_API_KEY": "sk-test", "SKILLSMITH_VISION_MODEL": "m1"})
    assert c.model == "m1" and "sk-test" not in repr(c)
    assert client_from_env({"ANTHROPIC_API_KEY": "sk-test"}).model == vision.DEFAULT_MODEL


def test_no_key_means_skipped_never_an_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bus, events = recording_bus()
    op = second_opinion(JPEG, JPEG, "blue cup on red cup", geometry_ok=True, bus=bus)
    assert op.skipped and op.ok is None and op.geometry_ok is True and not op.disputes_pass
    assert [e["type"] for e in events] == [vision.OPINION_EVENT]
    assert events[0]["data"]["skipped"] is True


# ---------------------------------------------------------------- second_opinion


def test_disagreeing_with_a_geometric_pass_is_flagged_not_overridden():
    client = FakeClient('{"ok": false, "reason": "The blue cup lies beside the red cup."}')
    bus, events = recording_bus()
    op = second_opinion(JPEG, PNG, "blue cup on red cup", geometry_ok=True, objects=["blue cup"], client=client, bus=bus)
    assert op.ok is False and op.geometry_ok is True and op.disputes_pass and op.advisory
    assert op.model == "fake-vision" and op.error is None
    images, prompt = client.calls[0]
    assert images == [("image/jpeg", JPEG), ("image/png", PNG)]  # before first, then after
    assert "blue cup" in prompt
    (event,) = events
    assert event["type"] == vision.OPINION_EVENT and event["data"]["source"] == "vision"
    assert "disputes geometry" in event["data"]["text"]
    assert event["data"]["disputes_pass"] is True and event["data"]["geometry_ok"] is True


def test_api_failure_is_an_event_not_an_exception():
    bus, events = recording_bus()
    op = second_opinion(JPEG, JPEG, "goal", geometry_ok=False, client=FakeClient(exc=TimeoutError("slow")), bus=bus)
    assert op.ok is None and "TimeoutError" in op.error and op.geometry_ok is False
    assert [e["type"] for e in events] == ["error", vision.OPINION_EVENT]
    assert events[0]["data"]["where"] == "perception.vision"


def test_garbled_reply_keeps_ok_none_with_an_error():
    op = second_opinion(JPEG, JPEG, "goal", client=FakeClient("hmm, hard to say"))
    assert op.ok is None and op.error == "no verdict in reply" and "hard to say" in op.reason


# ---------------------------------------------------------------- the HTTP client (urlopen patched)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_anthropic_client_request_and_reply(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["req"], seen["timeout"] = req, timeout
        body = {"content": [{"type": "text", "text": '{"ok": true, "reason": "stacked"}'}]}
        return _Resp(json.dumps(body).encode())

    monkeypatch.setattr(vision.urllib.request, "urlopen", fake_urlopen)
    client = AnthropicVisionClient("sk-test", model="m1", timeout_s=3.0)
    op = second_opinion(JPEG, JPEG, "blue cup on red cup", geometry_ok=True, client=client)
    assert (op.ok, op.reason, op.model) == (True, "stacked", "m1")
    req = seen["req"]
    assert req.full_url == vision.API_URL and seen["timeout"] == 3.0
    assert req.get_header("X-api-key") == "sk-test"
    body = json.loads(req.data)
    assert body["model"] == "m1"
    content = body["messages"][0]["content"]
    assert [c["type"] for c in content] == ["image", "image", "text"]
    assert base64.b64decode(content[0]["source"]["data"]) == JPEG


def test_anthropic_client_http_error_becomes_failed_opinion(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, io.BytesIO(b"bad key"))

    monkeypatch.setattr(vision.urllib.request, "urlopen", fake_urlopen)
    op = second_opinion(JPEG, JPEG, "goal", client=AnthropicVisionClient("sk-test"))
    assert op.ok is None and "401" in op.error and "sk-test" not in op.error


def test_anthropic_client_refuses_empty_key():
    with pytest.raises(ValueError):
        AnthropicVisionClient("")


# ---------------------------------------------------------------- arrays from crop_pair (needs cv2)


def test_numpy_crops_are_jpeg_encoded():
    pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    crop = np.zeros((96, 96, 3), dtype=np.uint8)
    client = FakeClient('{"ok": true, "reason": "fine"}')
    op = second_opinion(crop, crop, "goal", client=client)
    assert op.ok is True
    media_type, data = client.calls[0][0][0]
    assert media_type == "image/jpeg" and data.startswith(b"\xff\xd8")
