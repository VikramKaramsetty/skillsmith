# WS6: dashboard

**Branch prefix:** `ws6/` · **Owns:** `src/skillsmith/dashboard/`, `tests/ws6/`
**Reads:** `docs/INTERFACES.md` §7 (events, the ONLY input), `docs/DEMO.md`

## Goal
A big-screen page that tells the story live, entirely from the event stream: task →
attempt → camera FAIL → Droid thinking → diff → gates going green → Approve button →
real arm → SUCCESS → skill saved. It must also replay a recorded `events.jsonl`.

## Tasks (one PR each, in order)
- [ ] WS6.1 FastAPI server with an SSE event feed from the EventBus and replay of a recorded events.jsonl
- [ ] WS6.2 Single-page UI with no build step: timeline, Droid panel, code diff, gates checklist, result banner
- [ ] WS6.3 DashboardApprover that blocks the loop until Approve or Reject-with-note, plus a task input box
- [ ] WS6.4 Live camera panel (robotd MJPEG) and sim video player
- [ ] WS6.5 Replay mode for the demo fallback with speed control
- [ ] WS6.6 Big-screen polish: judge-readable type, current-step banner, dark theme

## Contract with the CLI (already wired)
- `skillsmith serve` calls `skillsmith.dashboard.server.main(cfg) -> int`.
- `--approver dashboard` constructs `skillsmith.dashboard.approver.DashboardApprover()`;
  it implements `request(run_id, summary) -> (approved, note)`.
- The server runs the `RepairLoop` in a worker thread and subscribes to its `EventBus`.

## Design notes
- One HTML file with vanilla JS (or Preact from a pinned CDN) served by FastAPI.
  No npm build, so any agent and any machine can change it.
- Render from events only. If the page needs data, add it to an event (request that from WS5/lead).
- Develop against `make demo-fake` runs: they write real `runs/*/events.jsonl` files.
- Diff: render the unified diff from `skill.diff` with simple +/- colouring.
- The Approve button must be big, and show the safety summary and sim video next to it.

## Acceptance (in `make check`)
- `tests/ws6/`: TestClient gets the page; the SSE endpoint streams events from a
  recorded file; approving via the HTTP endpoint unblocks a waiting `DashboardApprover`.
