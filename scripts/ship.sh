#!/usr/bin/env bash
# Commit, rebase on main, check, push, and open (or update) an auto-merging PR.
# Run from inside a workstream worktree whose branch is <ws>/<slug>.
#
#   scripts/ship.sh "ws2: build MuJoCo scene from Scene" [issue-number]
#
# Exit codes: 0 shipped, 1 checks failed, 2 rebase conflict (resolve by hand).
set -euo pipefail

TITLE=${1:?usage: ship.sh "<title>" [issue-number]}
ISSUE=${2:-}
BRANCH=$(git rev-parse --abbrev-ref HEAD)
WS=${BRANCH%%/*}
if [[ "$BRANCH" == "main" || "$BRANCH" == "$WS" ]]; then
  echo "ship: refusing to ship from '$BRANCH'; use a <ws>/<slug> branch" >&2
  exit 1
fi

git add -A
if ! git diff --cached --quiet; then
  git commit -q -m "$TITLE" ${ISSUE:+-m "Refs #$ISSUE"} ${COMMIT_TRAILER:+-m "$COMMIT_TRAILER"}
fi

git fetch -q origin main
if ! git rebase -q origin/main; then
  git rebase --abort
  echo "ship: rebase conflict with main. Resolve by hand, then re-run ship.sh." >&2
  exit 2
fi

make check || { echo "ship: make check failed" >&2; exit 1; }
uv run -q --no-project --with pyyaml python scripts/check_ownership.py --base origin/main || exit 1

git push -q -u origin HEAD --force-with-lease

BODY=$(mktemp)
if [[ -f HANDOFF.md ]]; then cat HANDOFF.md > "$BODY"; else cat .github/pull_request_template.md > "$BODY"; fi
if [[ -n "$ISSUE" ]]; then printf '\n\nCloses #%s\n' "$ISSUE" >> "$BODY"; fi

if gh pr view --json number >/dev/null 2>&1; then
  gh pr edit --body-file "$BODY" >/dev/null
  echo "ship: updated $(gh pr view --json url --jq .url)"
else
  gh pr create --title "$TITLE" --body-file "$BODY" --label automerge --label "$WS" --base main
fi
rm -f "$BODY"
