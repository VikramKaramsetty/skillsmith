"""Event bus. Every step of a run is an event; the dashboard renders from this stream alone.

Events go to runs/<run_id>/events.jsonl and to in-process subscribers (e.g. the SSE server).
Event types are listed in docs/INTERFACES.md §7.
"""

from __future__ import annotations

import dataclasses
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

Subscriber = Callable[[dict], None]


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def _jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _jsonable(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return repr(obj)


class EventBus:
    def __init__(self, run_id: str, runs_dir: Optional[Path] = None, echo: bool = False):
        self.run_id = run_id
        self.echo = echo
        self._subs: list[Subscriber] = []
        self._lock = threading.Lock()
        self._file = None
        self.run_dir: Optional[Path] = None
        if runs_dir is not None:
            self.run_dir = Path(runs_dir) / run_id
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self._file = open(self.run_dir / "events.jsonl", "a", buffering=1)

    def subscribe(self, fn: Subscriber) -> None:
        self._subs.append(fn)

    def emit(self, type_: str, **data: Any) -> dict:
        event = {"t": time.time(), "run_id": self.run_id, "type": type_, "data": _jsonable(data)}
        with self._lock:
            if self._file:
                self._file.write(json.dumps(event) + "\n")
            for fn in self._subs:
                try:
                    fn(event)
                except Exception as e:  # a broken subscriber must not stop the robot loop
                    print(f"[events] subscriber error: {e!r}")
        if self.echo:
            print(_format(event))
        return event

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None


def _format(event: dict) -> str:
    d = event["data"]
    if event["type"] == "primitive.call":
        args = ", ".join(f"{k}={v}" for k, v in d["args"].items() if v is not None and k != "text")
        status = "ok" if d["ok"] else f"FAILED {d['error']}"
        return f"      {d['index']:>3} {d['name']}({args}) -> {d['result'] if d['ok'] else ''} {status}"
    short = {k: v for k, v in d.items() if k not in ("scene", "diff", "source_code", "summary")}
    text = json.dumps(short)
    if len(text) > 160:
        text = text[:157] + "..."
    return f"  · {event['type']:<20} {text}"


class NullBus(EventBus):
    """For unit tests: records events in memory, writes nothing."""

    def __init__(self):
        super().__init__("test", runs_dir=None)
        self.events: list[dict] = []
        self.subscribe(self.events.append)

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]
