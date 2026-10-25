# Skillsmith: set up a new machine

You have been invited to **VikramKaramsetty/skillsmith**. This guide turns your computer into
a worker that takes tasks from GitHub, has an AI agent code them, and opens pull requests
that merge themselves when CI passes. About 10 minutes. macOS or Linux (on Windows, use
WSL2 with Ubuntu and run everything inside it).

---

## 1. Install the tools

| Tool | macOS | Linux (Ubuntu) |
|---|---|---|
| git, tmux | `brew install git tmux` | `sudo apt install -y git tmux make` |
| GitHub CLI | `brew install gh` | see https://github.com/cli/cli#installation |
| uv (Python) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | same |
| Claude Code | `curl -fsSL https://claude.ai/install.sh \| bash` | same |

**Factory Droid** (needed on machines that run the product's Droid repair step, i.e. the GX10
and anyone doing WS5 live runs; optional for build agents with `AGENT=droid`):

1. Install the CLI (the Python SDK drives it): `curl -fsSL https://app.factory.ai/cli | sh`
2. Create an API key at https://app.factory.ai/settings/api-keys (the account needs an active
   plan or hackathon credits; without them every call returns "402 No active subscription").
3. Put it in your shell profile, **never in the repo**:
   `echo 'export FACTORY_API_KEY=<your key>' >> ~/.zshrc` (Linux: `~/.bashrc`), then open a new terminal.
4. Check: `droid exec "Reply with ok" -o json` must show `"is_error":false`.

Open a new terminal afterwards so `uv` and `claude` are on your PATH.

## 2. Get access to the repo

```bash
gh auth login                      # pick GitHub.com, HTTPS, log in as the invited account
gh auth setup-git                  # lets git push with your gh login
gh api user/repository_invitations --jq '.[].id' | xargs -I{} gh api -X PATCH user/repository_invitations/{}
gh repo view VikramKaramsetty/skillsmith --json name   # should print {"name":"skillsmith"}
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"
```

## 3. Clone and check that everything works

```bash
gh repo clone VikramKaramsetty/skillsmith
cd skillsmith
make setup          # creates .venv with uv (downloads Python 3.12 if needed)
make check          # lint + tests: must end with "All checks passed!", "... passed", "OK"
make demo-fake      # full fail → repair → verify loop: must end with "SUCCESS after 1 repair round(s)"
```

If any of these fail, stop and message Vikram with the output.

## 4. Log in to the agent once

```bash
claude              # log in when asked, then type /exit
claude -p "reply with the word ready"   # headless test: should print "ready"
```

## 5. Start working the queue

```bash
tmux new -s agents 'POLL=300 caffeinate -i scripts/agent-loop.sh all'   # macOS
tmux new -s agents 'POLL=300 scripts/agent-loop.sh all'                 # Linux
```

Detach with `Ctrl-b` then `d`; the loop keeps running. Reattach with `tmux attach -t agents`.
`caffeinate -i` stops a Mac from sleeping mid-task.

What the loop does, over and over:
1. Finds the next free task. Only one task per workstream runs at a time across all
   machines, so tasks happen in order and agents never edit the same files.
2. Claims it on GitHub (a claim comment; if another machine claimed it first, it backs off).
3. Creates a git worktree in `../skillsmith-worktrees/` and runs the agent with the
   workstream's brief from `docs/workstreams/`.
4. Makes `make check` pass locally, then opens a PR.
5. Waits for CI. If CI fails, it feeds the log back to the agent (up to 3 times). Green →
   the PR merges itself → the issue closes → next task.

Options (put them before the command):

| Variable | Meaning | Default |
|---|---|---|
| `AGENT=droid` | use Factory Droid instead of Claude | `claude` |
| `POLL=300` | when nothing is free, wait 300 s and look again (omit to exit instead) | exit |
| `MODEL=<id>` | model for the agent | agent default |
| `MAX_FIX=3` | repair attempts for local and CI failures | 3 |

To work only certain areas instead of everything: `scripts/agent-loop.sh ws2 ws6`.

## 6. Watch it

```bash
make status                         # open PRs, in-progress, blocked, ready, hardware queue, CI
tail -f ../skillsmith-logs/*.jsonl  # raw agent transcripts
```

Or watch the PRs and issues on https://github.com/VikramKaramsetty/skillsmith.

## 7. Stop it

- **Gracefully:** `touch .agent-stop` in the repo folder. The loop finishes its current task
  and exits. Delete the file before starting again.
- **Right now:** reattach and press `Ctrl-C`. The task it was on stays `in-progress`; free it with
  `gh issue edit <number> --remove-label in-progress --add-label ready`.

## The arm on the Mac

Everything runs on the lead's Mac (Apple silicon): the arm, both cameras, robotd, sim, dashboard and Droid.
It's the default config (`config/robot.yaml`); `--profile stage` adds demo speed. Full detail: `docs/RUNBOOK.md`.

1. **LeRobot venv for robotd** (once; LeRobot's Feetech driver stays out of the project):
   ```bash
   uv venv --python 3.12 ~/robot/.venv
   uv pip install --python ~/robot/.venv/bin/python "lerobot[feetech]==0.6.1" fastapi uvicorn opencv-python pyyaml imageio-ffmpeg
   ```
2. **Arm.** Plug in the follower (12 V arm); `ls /dev/cu.*` shows `/dev/cu.usbmodem5AE60575661`
   (the config already uses it). The leader is only for teleop and calibration.
3. **Cameras.** Overhead: the iPhone through Continuity Camera (same Apple ID, Wi-Fi and Bluetooth on,
   iPhone locked and mounted; turn video effects such as Center Stage OFF). Wrist: the USB camera.
   `~/robot/.venv/bin/python tools/list_cameras.py` prints each camera's name and saves a frame to
   `runs/camera-probe/`; robotd finds them by name (`name:iPhone`, `name:USB2.0`).
4. **Arm calibration** (once per arm, about 10 min):
   `~/robot/.venv/bin/lerobot-calibrate --robot.type=so101_follower --robot.port=/dev/cu.usbmodem5AE60575661 --robot.id=follower`
5. **Run.** `~/robot/.venv/bin/python -m robotd --cameras-only` first (cameras, no arm), then without
   `--cameras-only`. In another terminal: `skillsmith --profile stage serve`.

## Rules

- **Never run anything against the real robot arm** (`--env real`, serial ports, cameras).
  Only the arm operator on the GX10 does hardware work. Tasks that need the arm are
  labelled `needs-hardware` and agents skip them.
- **Never push to `main`.** Everything goes through PRs; CI merges them.
- **Stay in your lane.** CI rejects a PR that edits files outside its workstream
  (`.github/ownership.yml`). Shared files (`types.py`, `docs/INTERFACES.md`, ...) change
  only through Vikram.
- One loop per machine is plenty. Each account has its own usage limits.

## Working by hand instead of the loop

```bash
git fetch origin && git worktree add -b ws3/my-change ../skillsmith-worktrees/ws3-my-change origin/main
cd ../skillsmith-worktrees/ws3-my-change && claude     # the .claude/agents/ws3-* subagent knows the lane
# ...work, write HANDOFF.md (format in AGENTS.md), then:
scripts/ship.sh "ws3: what I did" <issue-number>
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `make setup` can't find Python 3.12 | `uv python install 3.12`, then retry |
| `git push` → 403 / repository not found | the invite isn't accepted yet: re-run the invitation command in step 2 |
| loop exits with "Claude CLI is not ready" | the terminal CLI isn't logged in (separate from the desktop app): run `claude`, type `/login`, restart the loop |
| `claude: command not found` inside tmux | open a new terminal (PATH), or use the full path from `which claude` |
| loop prints "nothing free" | every workstream is busy or the queue is empty; use `POLL=300` to keep waiting |
| issue stuck `in-progress` after a crash | `gh issue edit <n> --remove-label in-progress --add-label ready` |
| issue labelled `blocked` | the agent gave up; leave it for Vikram (that workstream pauses until it's fixed) |
| `ship.sh` says "rebase conflict" | open the worktree, resolve the conflict, `git rebase --continue`, re-run `scripts/ship.sh` |

## More reading

`README.md` (overview) · `docs/NORTH_STAR.md` (objective, read first) · `docs/WORKFLOW.md` (how the pipeline works) ·
`AGENTS.md` (rules for agents) · `docs/workstreams/` (what each area builds) ·
`docs/DECISIONS.md` (decisions, hard truths, sources) · `docs/PLAN.md` (gates, time budget)
