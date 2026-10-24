"""Frontier-vision second opinion on cropped before/after images. OWNER: WS3b (WS3.10).

**Advisory only.** Geometry decides pass/fail (docs/NORTH_STAR.md); this module never changes
that verdict. It asks a frontier vision model "did the goal happen?" about the tight crops
from `annotate.crop_pair()` and returns a `VisionOpinion`: pass/fail plus one sentence. When
the model disputes a geometric *pass*, `opinion.disputes_pass` is set so the caller can add it
to the evidence (docs/ARCHITECTURE.md "Verification"); what to do with it is the caller's call.

- The API key comes from the environment (`ANTHROPIC_API_KEY`), never from a file in the repo.
  No key → `client_from_env()` returns None and `second_opinion()` returns a skipped opinion.
- Stdlib only (`urllib`): no SDK dependency. OpenCV is imported lazily only to JPEG-encode
  arrays; callers may pass JPEG/PNG bytes instead.
- The model is *not* told geometry's verdict, so its opinion is independent.
- Never raises on API trouble: an unreachable or garbled API gives `ok=None` with `error` set
  and an `error` event, so a stretch feature can never stop the robot loop.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Optional, Protocol, Sequence, Tuple, Union

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5-5"
KEY_ENV = "ANTHROPIC_API_KEY"
MODEL_ENV = "SKILLSMITH_VISION_MODEL"
TIMEOUT_S = 15.0
MAX_TOKENS = 200
# The opinion rides on the documented `note` event (text + the VisionOpinion fields, source="vision")
# until INTERFACES §7 gains a `vision.opinion` row (contract change request in the WS3.10 hand-off).
OPINION_EVENT = "note"

Image = Union[bytes, Any]  # encoded JPEG/PNG bytes, or a BGR numpy array


@dataclass(frozen=True)
class VisionOpinion:
    """One advisory verdict. `ok` is None when the model was skipped or gave no usable answer."""

    ok: Optional[bool]
    reason: str
    geometry_ok: Optional[bool] = None
    model: str = ""
    latency_s: float = 0.0
    error: Optional[str] = None
    skipped: bool = False
    advisory: bool = True  # always True: a reminder for every reader of the event

    @property
    def disputes_pass(self) -> bool:
        """Geometry said pass but the model says fail: extra evidence, never a verdict."""
        return self.geometry_ok is True and self.ok is False

    @property
    def agrees(self) -> Optional[bool]:
        if self.ok is None or self.geometry_ok is None:
            return None
        return self.ok == self.geometry_ok

    def to_dict(self) -> dict:
        return {**asdict(self), "disputes_pass": self.disputes_pass, "agrees": self.agrees}


class VisionClient(Protocol):
    model: str

    def ask(self, images: Sequence[Tuple[str, bytes]], prompt: str) -> str:
        """Send (media_type, bytes) images plus a prompt; return the model's text reply."""
        ...


# ---------------------------------------------------------------- prompt and reply (pure)


def build_prompt(goal: str, objects: Sequence[str] = ()) -> str:
    named = f" The objects involved: {', '.join(objects)}." if objects else ""
    return (
        "You are checking a robot arm's work on a tabletop. Image 1 is a crop of the table "
        "BEFORE the attempt, image 2 is the same crop AFTER it, from a fixed overhead camera.\n"
        f"Goal: {goal}.{named}\n"
        "Judge only from the images whether the goal is achieved in the AFTER image. "
        'Reply with JSON only: {"ok": true|false, "reason": "<one sentence, what you see>"}'
    )


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_reply(text: str) -> Tuple[Optional[bool], str]:
    """(ok, reason) from the model's reply; ok is None when there is no clear verdict."""
    match = _JSON_RE.search(text or "")
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("ok"), bool):
            return data["ok"], _one_sentence(str(data.get("reason", "")))
    return None, _one_sentence(text or "") or "no usable reply"


def _one_sentence(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- images


def encode_image(img: Image) -> Tuple[str, bytes]:
    """(media_type, bytes) for the API: bytes pass through, arrays are JPEG-encoded."""
    if isinstance(img, (bytes, bytearray)):
        data = bytes(img)
        return ("image/png" if data.startswith(b"\x89PNG") else "image/jpeg"), data
    import cv2

    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok:
        raise ValueError("could not JPEG-encode image")
    return "image/jpeg", buf.tobytes()


# ---------------------------------------------------------------- the API client


class AnthropicVisionClient:
    """Messages API over urllib. Construct with a key, or use `client_from_env()`."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout_s: float = TIMEOUT_S, url: str = API_URL):
        if not api_key:
            raise ValueError("AnthropicVisionClient needs an API key")
        self._key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.url = url

    def request_body(self, images: Sequence[Tuple[str, bytes]], prompt: str) -> dict:
        content: list[dict] = [
            {"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(b).decode()}}
            for mt, b in images
        ]
        content.append({"type": "text", "text": prompt})
        return {"model": self.model, "max_tokens": MAX_TOKENS, "messages": [{"role": "user", "content": content}]}

    def ask(self, images: Sequence[Tuple[str, bytes]], prompt: str) -> str:
        req = urllib.request.Request(
            self.url,
            data=json.dumps(self.request_body(images, prompt)).encode(),
            headers={"x-api-key": self._key, "anthropic-version": API_VERSION, "content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                reply = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"vision API HTTP {e.code}: {e.read()[:200]!r}") from e
        return "".join(b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text")

    def __repr__(self) -> str:  # never print the key
        return f"AnthropicVisionClient(model={self.model!r})"


def client_from_env(env: Optional[dict] = None) -> Optional[AnthropicVisionClient]:
    """A client when `ANTHROPIC_API_KEY` is set (model from `SKILLSMITH_VISION_MODEL`), else None."""
    env = os.environ if env is None else env
    key = env.get(KEY_ENV, "").strip()
    if not key:
        return None
    return AnthropicVisionClient(key, env.get(MODEL_ENV, "").strip() or DEFAULT_MODEL)


# ---------------------------------------------------------------- the entry point


def second_opinion(
    before: Image,
    after: Image,
    goal: str,
    *,
    geometry_ok: Optional[bool] = None,
    objects: Sequence[str] = (),
    client: Optional[VisionClient] = None,
    bus: Any = None,
) -> VisionOpinion:
    """Ask the vision model whether `goal` holds in the `after` crop (vs `before`).

    `client` defaults to `client_from_env()`; with no client the opinion is skipped. Emits the
    opinion on `bus` (if given, see `OPINION_EVENT`), plus an `error` event when the call fails.
    Never raises for API problems and never changes `geometry_ok`."""
    client = client if client is not None else client_from_env()
    if client is None:
        op = VisionOpinion(None, f"no vision client ({KEY_ENV} not set)", geometry_ok, skipped=True)
        return _emit(bus, op)
    model = getattr(client, "model", "")
    start = time.monotonic()
    try:
        images = [encode_image(before), encode_image(after)]
        text = client.ask(images, build_prompt(goal, objects))
    except Exception as e:  # advisory: report, never stop the loop
        latency = round(time.monotonic() - start, 3)
        op = VisionOpinion(None, "vision call failed", geometry_ok, model, latency, repr(e))
        if bus is not None:
            bus.emit("error", where="perception.vision", message=op.error)
        return _emit(bus, op)
    ok, reason = parse_reply(text)
    error = None if ok is not None else "no verdict in reply"
    return _emit(bus, VisionOpinion(ok, reason, geometry_ok, model, round(time.monotonic() - start, 3), error))


def summary(op: VisionOpinion) -> str:
    """One line for the dashboard, e.g. "vision (advisory): FAIL, disputes geometry's pass: ..."."""
    if op.skipped:
        return f"vision (advisory): skipped, {op.reason}"
    verdict = {True: "PASS", False: "FAIL", None: "no verdict"}[op.ok]
    dispute = ", disputes geometry's pass" if op.disputes_pass else ""
    return f"vision (advisory): {verdict}{dispute}: {op.reason}"


def _emit(bus: Any, op: VisionOpinion) -> VisionOpinion:
    if bus is not None:
        bus.emit(OPINION_EVENT, text=summary(op), source="vision", **op.to_dict())
    return op
