#!/usr/bin/env bash
# One-time GitHub setup, run by the lead from the repo root after the first commit.
# Creates the private repo, merge settings, labels, Actions permissions, collaborators
# (your other accounts) and the task issues.
#
#   scripts/setup-github.sh skillsmith  otherAccount1 otherAccount2
set -euo pipefail
NAME=${1:?usage: setup-github.sh <repo-name> [collaborator ...]}
shift
OWNER=$(gh api user --jq .login)
REPO="$OWNER/$NAME"
git rev-parse --verify -q HEAD >/dev/null || { echo "commit something first" >&2; exit 1; }

if gh repo view "$REPO" >/dev/null 2>&1; then
  echo "setup: $REPO exists"
else
  gh repo create "$REPO" --private --source . --remote origin --push
fi

gh repo edit "$REPO" --enable-squash-merge --enable-merge-commit=false --enable-rebase-merge=false \
  --delete-branch-on-merge --enable-auto-merge >/dev/null

# The automerge job merges with GITHUB_TOKEN, so workflows need write permission.
gh api -X PUT "repos/$REPO/actions/permissions/workflow" \
  -f default_workflow_permissions=write -F can_approve_pull_request_reviews=true

label() { gh label create "$1" --color "$2" --description "$3" --force -R "$REPO" >/dev/null; }
for ws in ws1 ws2 ws3 ws4 ws5 ws6 ws7 qa docs lead; do label "$ws" "1d76db" "workstream $ws"; done
label ready          "0e8a16" "queued for an agent"
label in-progress    "fbca04" "claimed by an agent-loop"
label blocked        "b60205" "needs a human"
label needs-hardware "d93f0b" "arm operator must run a hardware test"
label automerge      "5319e7" "merge automatically when CI is green"
label contract-change "e99695" "touches shared contracts; lead merges by hand"
label lead-approved  "c2e0c6" "skip the ownership check"

# Branch protection needs GitHub Pro/Team on private repos. Without it, the automerge
# job still only merges green PRs; protection additionally blocks direct pushes.
if gh api -X PUT "repos/$REPO/branches/main/protection" --input - >/dev/null 2>&1 <<'JSON'
{"required_status_checks": {"strict": false, "contexts": ["check", "heavy", "ownership"]},
 "enforce_admins": false, "required_pull_request_reviews": null, "restrictions": null}
JSON
then echo "setup: branch protection on main enabled"
else echo "setup: branch protection not available on this plan (fine; CI still gates automerge)"
fi

for user in "$@"; do
  gh api -X PUT "repos/$REPO/collaborators/$user" -f permission=push >/dev/null
  echo "setup: invited $user (they must accept: gh api user/repository_invitations)"
done

"$(dirname "$0")/seed-issues.sh" "$REPO"
echo "setup: done -> https://github.com/$REPO"
