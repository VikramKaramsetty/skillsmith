#!/usr/bin/env bash
# One-screen project board from GitHub: PRs, claimed/queued tasks, hardware queue, main CI.
set -uo pipefail
echo "== Open PRs";            gh pr list --limit 30
echo; echo "== In progress";   gh issue list --label in-progress --limit 30
echo; echo "== Blocked";       gh issue list --label blocked --limit 30
echo; echo "== Ready (next up)"; gh issue list --label ready --search "sort:created-asc" --limit 15
echo; echo "== Needs hardware (arm operator)"; gh issue list --label needs-hardware --limit 15
echo; echo "== CI on main";    gh run list --branch main --limit 5
