"""DashboardApprover. OWNER: WS6 (WS6.3). Implements `repair.protocols.Approver`.

Contract:
- `request(run_id, summary) -> (approved, note)` blocks until the dashboard's `POST /approve`
  resolves it, or `timeout_s` passes (counts as a reject with note "timed out").
- Two kinds of summary: a normal approval (`skill`, `version`, `source_code`, `sim`, `safety`,
  `video`) and `{"kind": "reset", "title": ...}` asking the human to reset the table.
- Must be thread-safe: the RepairLoop runs in a worker thread, HTTP handlers in the server thread.
- WS6.8: each request gets a fresh random `nonce`, shown only by `pending()` (never in an event, so a
  recording can't replay it). `decide(..., nonce=)` must quote it, so neither a stale page nor another
  site that can't read `GET /approval` can approve the arm.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from typing import Optional, Tuple

TIMED_OUT = "timed out"


@dataclass
class _Pending:
    run_id: str
    summary: dict
    nonce: str
    decision: Optional[Tuple[bool, str]] = None


class DashboardApprover:
    def __init__(self, timeout_s: float = 600.0):
        self.timeout_s = timeout_s
        self._cond = threading.Condition()
        self._pending: Optional[_Pending] = None

    def request(self, run_id: str, summary: dict) -> Tuple[bool, str]:
        """Block the loop's thread until a human decides. Silence is a reject, never an approve."""
        with self._cond:
            pending = self._pending = _Pending(run_id, dict(summary), secrets.token_urlsafe(16))
            self._cond.notify_all()
            self._cond.wait_for(lambda: pending.decision is not None, timeout=self.timeout_s)
            if self._pending is pending:
                self._pending = None
            return pending.decision or (False, TIMED_OUT)

    def pending(self) -> Optional[dict]:
        """The waiting request as {"run_id", "kind", "title", "nonce"}, or None."""
        with self._cond:
            p = self._pending
            if p is None:
                return None
            return {"run_id": p.run_id, "kind": p.summary.get("kind", "approval"),
                    "title": p.summary.get("title", ""), "nonce": p.nonce}

    def decide(self, approved: bool, note: str = "", run_id: Optional[str] = None,
               nonce: Optional[str] = None) -> bool:
        """Resolve the waiting request. False if nothing waits, it belongs to another run, or the
        nonce (when given; `POST /approve` always passes one) is not this request's."""
        with self._cond:
            p = self._pending
            if p is None or (run_id is not None and run_id != p.run_id):
                return False
            if nonce is not None and not secrets.compare_digest(str(nonce), p.nonce):
                return False
            p.decision = (bool(approved), note)
            self._pending = None
            self._cond.notify_all()
            return True

    def wait_pending(self, timeout_s: float) -> Optional[dict]:
        """Block until a request is waiting (for tests and scripts). None on timeout."""
        with self._cond:
            self._cond.wait_for(lambda: self._pending is not None, timeout=timeout_s)
        return self.pending()
