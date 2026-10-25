@AGENTS.md

## Claude Code specifics

- Workstream subagents live in `.claude/agents/`. The lead session dispatches them
  following `docs/WORKFLOW.md` (shared queue, worktrees, CI, contract changes).
- Run each workstream in its own git worktree (`isolation: "worktree"`) so parallel
  agents never edit the same checkout.
- Everything (robotd, the arm, cameras, sim, dashboard, Droid) runs on the lead's Mac.
  Agents may run read-only checks there (`ls /dev/cu.*`, `pgrep -a python`) but never open
  a serial port or a camera, and never start robotd without `--mock`. Hardware is human-only.
- Hardware facts and gotchas: `docs/RUNBOOK.md`.
