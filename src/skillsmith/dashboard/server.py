"""Dashboard server. OWNER: WS6 (docs/workstreams/WS6_dashboard.md).

Contract:
- `main(cfg) -> int`: called by `skillsmith serve`; serves on :8080 and blocks until Ctrl-C.
- `GET /`: the single-page UI (one HTML file, no build step).
- `GET /events`: SSE stream of events (docs/INTERFACES.md §7) from the live EventBus.
  `GET /events?replay=<run_id>&speed=2` replays runs/<run_id>/events.jsonl (demo fallback).
- `POST /task {"text": "..."}`: parse the task and start a RepairLoop in a worker thread.
- `POST /approve {"approved": bool, "note": str}`: resolves the pending DashboardApprover request.
- The UI renders ONLY from events. If it needs more data, request a new event field from the lead.
"""

from __future__ import annotations


def main(cfg) -> int:
    print("dashboard not built yet (WS6): see docs/workstreams/WS6_dashboard.md")
    return 1
