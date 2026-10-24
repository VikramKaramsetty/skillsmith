#!/usr/bin/env bash
# Work the GitHub issue queue, one task at a time.
#
#   scripts/agent-loop.sh all                    # shared queue: take ANY workstream's next task
#   scripts/agent-loop.sh ws2 ws6                # only these workstreams
#   POLL=300 AGENT=droid scripts/agent-loop.sh all   # keep waiting for new issues
#
# Safe to run `all` on every machine at once:
#   - One task in flight per workstream: a workstream with an in-progress or blocked issue is
#     skipped, so its tasks run in order and two agents never edit the same files.
#   - Claims are decided by comment order on the issue (GitHub orders comments), so exactly one
#     machine wins a race for the same task.
#   - `touch .agent-stop` in the repo root stops the loop after the current task.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"

ALL_WS="ws1 ws2 ws3a ws3b ws3c ws4 ws5 ws6 ws7 qa"  # fallback; `all` reads main's ownership.yml each pass
HOST=${HOST_TAG:-$(hostname -s 2>/dev/null || hostname)}
MARK="🤖 claim"
log() { echo "agent-loop[$HOST]: $*"; }

workstreams() {  # every queue in main's ownership.yml (minus docs), so a new or split workstream reaches running loops
  local ws
  git fetch -q origin main 2>/dev/null || true
  ws=$(git show origin/main:.github/ownership.yml 2>/dev/null | sed -n 's/^  \([a-z0-9]*\):$/\1/p' | grep -vx docs | tr '\n' ' ')
  echo "${ws:-$ALL_WS}"
}

rotate() {  # start each machine at a different workstream so they spread out instead of racing
  local a=("$@") n=$# i off
  off=$(( $(printf '%s' "$HOST" | cksum | cut -d' ' -f1) % n ))
  for ((i = 0; i < n; i++)); do printf '%s\n' "${a[$(( (i + off) % n ))]}"; done
}

busy() {  # a workstream is busy while any of its open issues is in-progress or blocked
  local n
  n=$(gh issue list --label "$1" --state open --limit 100 --json labels \
        --jq '[.[] | select(any(.labels[]; .name == "in-progress" or .name == "blocked"))] | length') || return 0  # unreachable = treat as busy
  [[ "$n" -gt 0 ]]
}

oldest_ready() {
  gh issue list --label "$1" --label ready --state open --limit 1 --search "sort:created-asc" \
    --json number,title --jq '.[0] | select(.) | "\(.number)\t\(.title)"'
}

claim() {  # succeeds only for the machine whose claim comment came first (within 60 s)
  local num=$1 token winner
  token="$MARK $HOST $$ $RANDOM"
  gh issue comment "$num" --body "$token" >/dev/null
  sleep "${CLAIM_WAIT:-5}"
  winner=$(gh api "repos/{owner}/{repo}/issues/$num/comments?per_page=100" --jq "
    [.[] | select(.body | startswith(\"$MARK\"))] as \$c
    | (\$c | map(select(.body == \"$token\"))[0].created_at | fromdateiso8601) as \$t
    | \$c | map(select((.created_at | fromdateiso8601) >= \$t - 60))[0].body")
  [[ "$winner" == "$token" ]] || return 1
  gh issue edit "$num" --add-assignee @me --remove-label ready --add-label in-progress >/dev/null
}

release() {
  gh issue edit "$1" --remove-assignee @me --remove-label in-progress --add-label ready >/dev/null
}

preflight() {  # fail fast instead of burning the queue: every tool must be logged in
  gh auth status >/dev/null 2>&1 || { log "gh is not logged in: run 'gh auth login'"; exit 1; }
  case "${AGENT:-claude}" in
    claude)
      local out
      out=$(claude -p "Reply with the word ok." --output-format json 2>&1 || true)
      if ! printf '%s' "$out" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("is_error") is False else 1)' 2>/dev/null; then
        log "Claude CLI is not ready: $(printf '%s' "$out" | head -c 200)"
        log "Fix: run 'claude' once, type /login, then start the loop again."
        exit 1
      fi ;;
    droid)
      command -v droid >/dev/null || { log "droid CLI not found (and set FACTORY_API_KEY)"; exit 1; } ;;
  esac
}

main() {
[[ $# -eq 0 ]] && { sed -n '2,14p' "$0"; exit 1; }
cd "$(git rev-parse --show-toplevel)"
DYNAMIC=""; if [[ "$1" == "all" ]]; then DYNAMIC=1; set -- $(workstreams); fi
preflight
log "ready: ${AGENT:-claude} on $*"

while true; do
  if [[ -f .agent-stop ]]; then log ".agent-stop found; stopping"; exit 0; fi

  [[ -n "$DYNAMIC" ]] && set -- $(workstreams)
  NUM=""
  for ws in $(rotate "$@"); do
    busy "$ws" && continue
    NEXT=$(oldest_ready "$ws") || { log "GitHub unreachable; skipping $ws this round"; continue; }
    [[ -z "$NEXT" ]] && continue
    IFS=$'\t' read -r num title <<< "$NEXT"
    if ! claim "$num"; then log "lost the race for #$num; trying another workstream"; continue; fi
    # Rare: two machines claimed DIFFERENT tasks of one workstream at the same moment. Keep the oldest.
    if [[ $(gh issue list --label "$ws" --label in-progress --state open --json number \
              --jq "[.[] | select(.number < $num)] | length") -gt 0 ]]; then
      log "another task in $ws is already running; releasing #$num"; release "$num"; continue
    fi
    NUM=$num WS=$ws TITLE=$title
    break
  done

  if [[ -z "$NUM" ]]; then
    if [[ -n "${POLL:-}" ]]; then log "nothing free; waiting ${POLL}s"; sleep "$POLL"; continue; fi
    log "nothing free for: $*"; exit 0
  fi

  log "working #$NUM ($WS) $TITLE"
  started=$SECONDS
  set +e
  scripts/agent-run.sh "$WS" "$TITLE" --issue "$NUM"
  rc=$?
  set -e
  # A run that dies within FAST_FAIL_S seconds never got going (stale branch/worktree, uv, agent CLI
  # logged out or rate-limited): that is this machine's problem, not the task's. Hand the task back
  # and stop, instead of blocking one workstream after another.
  if [[ $rc -ne 0 && $rc -ne 3 && $(( SECONDS - started )) -lt ${FAST_FAIL_S:-90} ]]; then
    release "$NUM" || true
    gh issue comment "$NUM" --body "agent-loop on $HOST: the run failed after $(( SECONDS - started )) s (exit $rc), before the agent did any work. Released back to \`ready\`; this machine's loop stopped. Log: ../skillsmith-logs/" >/dev/null || true
    log "run failed in $(( SECONDS - started ))s (exit $rc): machine problem; released #$NUM and stopping. Check the log, then restart."
    exit 1
  fi
  if [[ $rc -eq 0 ]]; then
    gh issue edit "$NUM" --remove-label in-progress >/dev/null || true
  else
    reason="the agent run did not finish cleanly (exit $rc)"
    [[ $rc -eq 3 ]] && reason="the agent made no changes (task unclear, already done, or the agent CLI failed)"
    gh issue edit "$NUM" --remove-label in-progress --add-label blocked >/dev/null || true
    gh issue comment "$NUM" --body "agent-loop on $HOST: $reason. Needs a human; $WS is paused until this is unblocked (relabel \`ready\` or close). Log: ../skillsmith-logs/" >/dev/null || true
  fi
done
}

# Run when executed; when sourced (tests), only define the functions.
if [[ "${BASH_SOURCE[0]:-}" == "$0" ]]; then main "$@"; fi
