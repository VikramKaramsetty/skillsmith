# WS6: dashboard

**Branch prefix:** `ws6/` · **Owns:** `src/skillsmith/dashboard/`, `tests/ws6/` · **Reads:** `docs/NORTH_STAR.md`, `docs/INTERFACES.md` §7 (events, the ONLY input), `docs/DEMO.md`

## Goal
A big-screen page that tells the story live from the event stream alone: task → attempt →
FAIL with the failure class → Droid's diagnosis, steps and diff → gates go green → Approve →
real success → skill saved. Judges get one sentence per step; detail sits behind a click. It
also replays a recorded run for the fallback.
Serves NORTH_STAR: 3.

## Tasks (one PR each, in order)
- [ ] WS6.1 FastAPI server with an SSE event feed and replay of a recorded events.jsonl
- [ ] WS6.2 Big-screen view: task, status banner with failure class, Droid's one-line diagnosis, short diff, gate lights, Approve, PASS
- [ ] WS6.3 DashboardApprover and task input
- [ ] WS6.4 Detail drawer: annotated key frames, phase table, Droid tool calls and permission decisions
- [ ] WS6.5 Live camera with world-state overlay and the sim video
- [ ] WS6.6 Improvement panel: rounds, seconds-to-fix, skills saved, reuse and lessons
- [ ] WS6.7 Replay mode with speed control for the fallback

## Contract with the CLI (already wired)
- `skillsmith serve` calls `skillsmith.dashboard.server.main(cfg) -> int` (stub exists).
- `--approver dashboard` builds `skillsmith.dashboard.approver.DashboardApprover()` with
  `request(run_id, summary) -> (approved, note)`; thread-safe (loop in a worker thread).

## Design notes
- **Render only from events** (INTERFACES §7). Need more data? Request a new event field from the lead.
- Big screen = one current step in large type: the banner uses `attempt.classified.summary`,
  Droid's line uses `droid.result.result.diagnosis`, the diff comes from `skill.diff` (a few lines,
  coloured), gate lights from `gate.result`. Everything else goes in the drawer.
- Show at least one refused `droid.permission` in the drawer: that is the "controlled touch" proof.
- One HTML file, vanilla JS (or a pinned CDN library), served by FastAPI. No npm build.
- Develop against real logs from `make demo-fake` (`runs/*/events.jsonl`).

## Acceptance (in `make check`)
- `tests/ws6/`: TestClient serves the page; SSE streams a recorded file in order; approving over
  HTTP unblocks a waiting `DashboardApprover`; replay speed control works.
