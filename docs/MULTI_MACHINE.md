# Building on several machines at once

GitHub is the hub. Every machine runs agents that take tasks from the issue queue, work
in their own git worktree, and open pull requests that **merge themselves** once CI is
green. Nobody waits for anybody, and no two agents ever edit the same file.

```
                       ┌──────────── GitHub (private repo) ─────────────┐
                       │ issues = task queue    PRs + CI = merge gate   │
                       │ labels: ws1..ws7 qa · ready · in-progress ·    │
                       │ blocked · needs-hardware · automerge           │
                       └───▲──────────▲──────────▲──────────▲───────────┘
          claim, PR, merge │          │          │          │ pull main, run hardware tests
 ┌─────────────────────┐ ┌─┴────────┐ ┌┴─────────┐ ┌────────┴──────────────┐
 │ Mac A (lead)        │ │ Machine B│ │ Machine C│ │ GX10 + arm operator   │
 │ agent-loop ws4 ws5  │ │ loop ws2 │ │ loop ws3 │ │ issues: needs-hardware│
 │ + merges contracts  │ │      ws6 │ │  ws1 ws7 │ │ robotd, real runs     │
 └─────────────────────┘ └──────────┘ └──────────┘ └───────────────────────┘
```

## How one task flows

1. The lead seeds issues from the `- [ ] WSn.m ...` lines in `docs/workstreams/*.md`
   (`scripts/seed-issues.sh`). Each issue is labelled with its workstream and `ready`.
2. `scripts/agent-loop.sh ws2` on machine B takes the **oldest** `ready` ws2 issue,
   assigns it to itself and relabels it `in-progress`.
3. `scripts/agent-run.sh` creates a worktree on branch `ws2/<issue>-<slug>` from the
   latest `origin/main` and runs a headless agent (`claude -p` or `droid exec`) with
   the workstream brief.
4. Fix loop: `make check` must pass locally. Failures go back to the same agent session
   (up to `MAX_FIX` rounds).
5. `scripts/ship.sh`: commit → rebase on `origin/main` → `make check` → ownership check
   → push → open a PR with the agent's `HANDOFF.md` as the body, labelled `automerge` + `ws2`.
6. CI runs `check` (lint + tests), `heavy` (tests with mujoco/placo/opencv) and
   `ownership` (the branch touched only ws2 paths). All green → the `automerge` job
   squash-merges the PR and deletes the branch. "Closes #N" closes the issue.
7. If CI goes red, `agent-run.sh` pulls the failed log, hands it to the agent, and pushes
   the fix. After `MAX_FIX` rounds it gives up, leaves the PR open, and labels the issue `blocked`.
8. The loop takes the next issue, starting from the new `main`.

## Why it doesn't collide

- **Ownership:** `.github/ownership.yml` maps each branch prefix to the paths it may
  edit. CI enforces it, so parallel PRs almost never conflict.
- **One task in flight per workstream.** The loop skips any workstream that has an
  `in-progress` or `blocked` issue, so a workstream's tasks run in order (WS2.3 starts
  only after WS2.2 has merged), whichever machine picks them up.
- **Safe claims.** A machine claims an issue by posting a claim comment, waits 5 s, and
  keeps the issue only if its comment came first. GitHub orders comments, so exactly one
  machine wins; losers move on to another workstream.
- **Shared contracts** (`types.py`, `hal/base.py`, `primitives.py`, `events.py`,
  `config/robot.yaml`, `docs/INTERFACES.md`, ...) change only on `lead/*` branches. The
  `contract-change` label blocks automerge, so the lead merges those by hand.
- **Small PRs.** One task = one PR, ideally < 300 lines. Rebasing small PRs is cheap.

## One-time setup (lead, on Mac A)

```bash
git init && git add -A && git commit -m "Skillsmith scaffold"
scripts/setup-github.sh skillsmith OTHER_ACCOUNT_1 OTHER_ACCOUNT_2
```

This creates the private repo, sets squash-only merges and delete-on-merge, gives
Actions write access (needed by the automerge job), creates the labels, tries branch
protection (needs GitHub Pro/Team for private repos; without it the automerge job still
merges only green PRs), invites your other accounts, and seeds the issues.

## Each extra machine (once)

```bash
gh auth login                                    # the account you invited
gh api user/repository_invitations --jq '.[].id' | xargs -I{} gh api -X PATCH user/repository_invitations/{}
gh repo clone <owner>/skillsmith && cd skillsmith
curl -LsSf https://astral.sh/uv/install.sh | sh  # if uv is missing
make setup && make check
claude                                           # log in once (or install droid and export FACTORY_API_KEY)
```

Then start the loop in tmux so it survives disconnects:

```bash
tmux new -s agents 'POLL=300 scripts/agent-loop.sh all'
```

Use `AGENT=droid` for Droid, `POLL=300` to keep waiting for new issues, `MODEL=...` to
pick a model, `MAX_FIX=3` for the repair budget. `touch .agent-stop` stops a loop after
its current task. Logs: `../skillsmith-logs/*.jsonl`.

## Shared queue (recommended) or a fixed split

**Shared queue:** run `scripts/agent-loop.sh all` on every machine. No assignment
needed: each machine starts at a different workstream and takes whatever is free. With
8 workstreams and 4 machines, every machine stays busy until the queue drains.
Hardware tasks (`needs-hardware`) are never taken by agents; the arm operator does them
on the GX10.

**Fixed split** (if you want a machine to own an area, e.g. the only one with a GPU):
`scripts/agent-loop.sh ws2 ws6`. You can mix both: one machine on `ws2` only, the rest on `all`.

Each Claude/Droid account has its own usage limits, so spreading the agents across
accounts also multiplies throughput.

## The lead's job (about 10 minutes an hour)

- `make status` shows open PRs, claimed, blocked and ready tasks, the hardware queue and CI on main.
- Unblock `blocked` issues: read the PR and the log, then re-brief by editing the issue,
  relabel it `ready`, or take it over.
- Merge `contract-change` PRs by hand. Then tell the running loops nothing: their next
  task starts from the new `main` anyway.
- File follow-ups as new issues (`gh issue create --label ws3 --label ready ...`) or add
  task lines to a WS file and re-run `scripts/seed-issues.sh` (it skips existing titles).
- If `main` breaks after a merge, fix it at once on a `lead/` branch. Everything else
  depends on a green `main`.

## Working interactively instead

Humans can work the same way by hand: create a worktree on a `wsN/...` branch, work in
Claude Code (the `.claude/agents/wsN-*` subagents know their lane), write `HANDOFF.md`,
then run `scripts/ship.sh "wsN: what I did" <issue>`.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `ship.sh` exits 2 | rebase conflict with main | resolve in the worktree, `git rebase --continue`, re-run ship |
| ownership job red | edited a file outside the lane | move the change to the owner, or the lead labels `lead-approved` |
| PR green but not merged | missing `automerge` label, or Actions lacks write permission | add the label; re-run `setup-github.sh` |
| two machines on one issue | the same workstream on two machines | one workstream per machine |
| agent made no changes | task unclear or already done | improve the issue text, relabel `ready` |
