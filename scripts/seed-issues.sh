#!/usr/bin/env bash
# Create one GitHub issue per task line in docs/workstreams/*.md. Safe to re-run
# (skips titles that already exist). Task line format:
#   - [ ] WS2.1 Vendor the SO101 model and build the scene        (labels: ws2, ready)
#   - [ ] WS1.3 Hardware smoke test of robotd (hardware)            (+ needs-hardware)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
REPO=${1:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}
EXISTING=$(gh issue list -R "$REPO" --state all --limit 1000 --json title --jq '.[].title')

for f in docs/workstreams/*.md; do
  ws=$(basename "$f" | cut -d_ -f1 | tr '[:upper:]' '[:lower:]')   # WS2_x.md -> ws2, QA_x.md -> qa
  grep -E '^- \[ \] [A-Z]+[0-9]*\.[0-9]+ ' "$f" | while IFS= read -r line; do
    title=${line#- \[ \] }
    if printf '%s\n' "$EXISTING" | grep -qxF "$title"; then continue; fi
    labels="$ws,ready"
    case "$title" in *"(hardware)"*) labels="$ws,needs-hardware" ;; esac
    body=$(printf 'Workstream brief: https://github.com/%s/blob/main/%s\n\nAgents: `scripts/agent-loop.sh %s` picks this up automatically.\nHuman: `scripts/agent-run.sh %s "%s" --issue <this number>`.' \
      "$REPO" "$f" "$ws" "$ws" "$title")
    gh issue create -R "$REPO" --title "$title" --label "$labels" --body "$body" >/dev/null
    echo "seed: $title"
  done
done
