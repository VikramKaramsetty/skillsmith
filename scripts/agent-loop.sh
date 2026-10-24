#!/usr/bin/env bash
# Work the GitHub issue queue for the given workstreams, oldest first, one task at a time.
# Run one loop per machine, and give each workstream to ONE machine only.
#
#   scripts/agent-loop.sh ws2 ws6                 # exit when the queue is empty
#   POLL=300 AGENT=droid scripts/agent-loop.sh ws4  # keep polling every 5 min
set -euo pipefail
[[ $# -eq 0 ]] && { sed -n '2,7p' "$0"; exit 1; }
cd "$(git rev-parse --show-toplevel)"

next_issue() {
  for ws in "$@"; do
    gh issue list --label "$ws" --label ready --state open --limit 1 \
      --search "sort:created-asc" --json number,title \
      --jq '.[0] | select(.) | "\(.number)\t'"$ws"'\t\(.title)"'
  done | head -1
}

while true; do
  NEXT=$(next_issue "$@")
  if [[ -z "$NEXT" ]]; then
    if [[ -n "${POLL:-}" ]]; then echo "agent-loop: queue empty; waiting ${POLL}s"; sleep "$POLL"; continue; fi
    echo "agent-loop: queue empty for $*"; exit 0
  fi
  IFS=$'\t' read -r NUM WS TITLE <<< "$NEXT"
  echo "agent-loop: claiming #$NUM ($WS) $TITLE"
  gh issue edit "$NUM" --add-assignee @me --remove-label ready --add-label in-progress >/dev/null
  if scripts/agent-run.sh "$WS" "$TITLE" --issue "$NUM"; then
    gh issue edit "$NUM" --remove-label in-progress >/dev/null || true
  else
    gh issue edit "$NUM" --remove-label in-progress --add-label blocked >/dev/null
    gh issue comment "$NUM" --body "agent-loop on $(hostname): agent run did not finish cleanly; needs a human." >/dev/null
  fi
done
