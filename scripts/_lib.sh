#!/usr/bin/env bash
# Shared helpers for the agent scripts. Sourced, not run.

# Retry GitHub CLI calls: a network blip must not kill a loop that runs for hours.
# 4 tries with 5/10/15 s backoff. Calls are safe to repeat (label edits are idempotent;
# a duplicate claim comment carries the same token, so it doesn't change who wins).
gh() {
  local i
  for i in 1 2 3 4; do
    command gh "$@" && return 0
    [[ $i -lt 4 ]] && { echo "gh: attempt $i failed; retrying in $((i * 5))s" >&2; sleep $((i * 5)); }
  done
  return 1
}
