@AGENTS.md

## Claude Code specifics

- Workstream subagents live in `.claude/agents/`. The lead session dispatches them
  following `docs/WORKFLOW.md` (shared queue, worktrees, CI, contract changes).
- Run each workstream in its own git worktree (`isolation: "worktree"`) so parallel
  agents never edit the same checkout.
- The GX10 is reachable at `ssh asus@100.103.96.17` (Tailscale). Agents may run
  read-only commands there (`ls`, `cat`, `nvidia-smi`, `pgrep -a`) and `rsync` code to
  `~/skillsmith/`. Anything that touches serial ports or cameras is human-only.
- Hardware facts and gotchas: `docs/RUNBOOK_GX10.md`.
