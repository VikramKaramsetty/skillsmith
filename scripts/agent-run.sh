#!/usr/bin/env bash
# Run ONE headless coding agent on ONE task in its own worktree, then ship a PR and
# babysit CI until it merges (feeding failures back to the agent).
#
#   scripts/agent-run.sh ws2 "WS2.1 Vendor the SO101 model and build the scene"
#   scripts/agent-run.sh ws4 "WS4.2 Harden validator" --agent droid --issue 7
#
# Env: AGENT=claude|droid (default claude), MAX_FIX=3, MODEL=<id> (optional)
# Needs: git, uv, gh (logged in), and `claude` or `droid` (FACTORY_API_KEY set).
set -euo pipefail

usage() { sed -n '2,10p' "$0"; exit 1; }
WS=${1:-}; TASK=${2:-}
[[ -z "$WS" || -z "$TASK" ]] && usage
shift 2
AGENT=${AGENT:-claude}; ISSUE=""; MAX_FIX=${MAX_FIX:-3}
while [[ $# -gt 0 ]]; do
  case $1 in
    --agent) AGENT=$2; shift 2 ;;
    --issue) ISSUE=$2; shift 2 ;;
    *) usage ;;
  esac
done

ROOT=$(git rev-parse --show-toplevel)
SLUG=$(printf '%s' "$TASK" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | cut -c1-40 | sed 's/^-//; s/-$//')
BRANCH="$WS/${ISSUE:+$ISSUE-}$SLUG"
NAME=$(printf '%s' "$BRANCH" | tr '/' '-')
WT="$ROOT/../skillsmith-worktrees/$NAME"
LOGS="$ROOT/../skillsmith-logs"; mkdir -p "$LOGS"
LOG="$LOGS/$NAME.jsonl"
WS_UPPER=$(printf '%s' "$WS" | tr '[:lower:]' '[:upper:]')
WSFILE=$(cd "$ROOT" && ls docs/workstreams/"${WS_UPPER}"_*.md 2>/dev/null | head -1 || true)

echo "agent-run: $AGENT on $BRANCH (log: $LOG)"
git -C "$ROOT" fetch -q origin main
if [[ -d "$WT" ]]; then echo "agent-run: reusing $WT"; else
  git -C "$ROOT" worktree add -q -b "$BRANCH" "$WT" origin/main
fi
cd "$WT"
uv sync -q --extra dev

PROMPT="You are a coding agent on the Skillsmith hackathon project (robot arm that writes its own skills).
Workstream: $WS. Task: $TASK.${ISSUE:+ GitHub issue #$ISSUE.}
1. Read AGENTS.md, docs/INTERFACES.md${WSFILE:+ and $WSFILE} before writing code.
2. Edit ONLY the paths listed for '$WS' in .github/ownership.yml, plus tests/$WS/. Shared files belong to the lead:
   if you need one changed, describe it under CONTRACT CHANGE REQUEST in HANDOFF.md instead.
3. Never touch real hardware (no serial ports, cameras, or --env real). Use the fake or sim env.
4. Do NOT commit or push; the runner does that.
5. Work until 'make check' passes. Keep the change focused on this one task.
6. Finish by writing HANDOFF.md in the repo root using the hand-off note format from AGENTS.md (it becomes the PR body)."

run_agent() {  # $1 = prompt, $2 = "continue" to resume the previous session
  case $AGENT in
    claude)
      claude -p "$1" ${2:+--continue} ${MODEL:+--model "$MODEL"} \
        --permission-mode acceptEdits \
        --allowedTools "Read,Edit,Write,Glob,Grep,Bash(make:*),Bash(uv:*),Bash(python3:*),Bash(git status:*),Bash(git diff:*),Bash(git log:*),Bash(ls:*),Bash(cat:*)" \
        --output-format stream-json --verbose >> "$LOG" ;;
    droid)
      droid exec --auto medium --cwd "$WT" -o stream-json ${MODEL:+-m "$MODEL"} "$1" >> "$LOG" ;;
    *) echo "unknown AGENT=$AGENT" >&2; exit 1 ;;
  esac
}

export COMMIT_TRAILER=""
[[ $AGENT == claude ]] && COMMIT_TRAILER="Co-Authored-By: Claude <noreply@anthropic.com>"
[[ $AGENT == droid ]] && COMMIT_TRAILER="Co-Authored-By: Factory Droid <droid@factory.ai>"

run_agent "$PROMPT"

# Local fix loop: make check must pass before anything is pushed.
for i in $(seq 1 "$MAX_FIX"); do
  if OUT=$(make check 2>&1); then break; fi
  echo "agent-run: make check failed (fix round $i)"
  run_agent "make check fails. Fix it without leaving your owned paths. Output (tail):
$(printf '%s' "$OUT" | tail -60)" continue
done

if git diff --quiet && git diff --cached --quiet && [[ -z $(git ls-files --others --exclude-standard) ]]; then
  echo "agent-run: agent made no changes; nothing to ship"; exit 0
fi

TITLE="$WS: $TASK"
for i in $(seq 0 "$MAX_FIX"); do
  set +e; scripts/ship.sh "$TITLE" "$ISSUE"; rc=$?; set -e
  if [[ $rc -eq 2 ]]; then echo "agent-run: rebase conflict; a human must resolve $WT"; exit 2; fi
  if [[ $rc -ne 0 ]]; then
    run_agent "Shipping failed (make check or the ownership check). Fix it. See the last command output in your terminal log." continue
    continue
  fi
  sleep 15  # let GitHub register the checks
  if gh pr checks --watch --interval 20 >/dev/null 2>&1; then
    echo "agent-run: CI green; automerge will merge it: $(gh pr view --json url --jq .url)"
    exit 0
  fi
  RUN_ID=$(gh run list --branch "$BRANCH" --limit 1 --json databaseId --jq '.[0].databaseId')
  FAIL=$(gh run view "$RUN_ID" --log-failed 2>/dev/null | tail -80)
  echo "agent-run: CI failed (round $i); sending the log to the agent"
  run_agent "CI failed on the PR. Fix it. Failed log (tail):
$FAIL" continue
done
echo "agent-run: gave up after $MAX_FIX CI rounds; PR left open for a human"
exit 1
