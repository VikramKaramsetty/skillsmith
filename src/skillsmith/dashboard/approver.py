"""DashboardApprover. OWNER: WS6 (WS6.3). Implements `repair.protocols.Approver`.

Contract:
- `request(run_id, summary) -> (approved, note)` blocks until the dashboard's `POST /approve`
  resolves it, or `timeout_s` passes (counts as a reject with note "timed out").
- Two kinds of summary: a normal approval (`skill`, `version`, `source_code`, `sim`, `safety`,
  `video`) and `{"kind": "reset", "title": ...}` asking the human to reset the table.
- Must be thread-safe: the RepairLoop runs in a worker thread, HTTP handlers in the server thread.
"""

from __future__ import annotations

from typing import Tuple


class DashboardApprover:
    def __init__(self, timeout_s: float = 600.0):
        raise NotImplementedError("WS6.3: see docs/workstreams/WS6_dashboard.md")

    def request(self, run_id: str, summary: dict) -> Tuple[bool, str]:
        raise NotImplementedError("WS6.3")
