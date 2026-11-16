#!/usr/bin/env bash
# Godmode: puts Claude's policies back to what they were before the last apply. Fast emergency
# route; the normal way to undo a change is a revert PR, then apply.
#
#   via the "apply" GitHub Actions workflow with action "rollback" (after approval), or in CloudShell:
#   cd ~/aws-build-runner/part1-godmode && ./rollback.sh
#
# For each of the three policies: makes the previous IAM version the default again. A policy with
# no previous version (it was new in the last apply) is detached from Claude's user instead
# (the boundary has nothing to detach, so it stays). Then it records what happened:
#   - IAM tags on each policy: applied-commit = rolled-back-from-<commit>, applied-at, applied-by
#   - GitHub: the last successful "aws" deployment is marked inactive, and an issue asks for the
#     change to be reverted in git, so main matches AWS again.
# check-live.py reports drift until that revert is merged.
set -Eeuo pipefail
cd "$(dirname "$0")"

ACCOUNT_ID="$(python3 -I config.py AWS_ACCOUNT_ID)"
USER_NAME="$(python3 -I config.py AWS_CLAUDE_USER)"
BOUNDARY="$(python3 -I -c "import yaml; print(yaml.safe_load(open('policy.yaml'))['iam']['boundary'])")"
IN_ACTIONS="${GITHUB_ACTIONS:-false}"
if [[ "$IN_ACTIONS" == true ]]; then
  who="${GITHUB_ACTOR} via GitHub Actions run ${GITHUB_RUN_ID}"; REPO="${GITHUB_REPOSITORY}"
else
  who="$(gh api user --jq .login) as $(aws sts get-caller-identity --query Arn --output text)"
  REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
fi
[[ "$(aws sts get-caller-identity --query Arn --output text)" != *":user/${USER_NAME}" ]] \
  || { echo "Refusing: run this as admin, not as Claude's user." >&2; exit 1; }

arn_of() { echo "arn:aws:iam::${ACCOUNT_ID}:policy/$1"; }
from_commit="$(aws iam list-policy-tags --policy-arn "$(arn_of claude-assistant-policy)" \
  --query "Tags[?Key=='applied-commit'].Value | [0]" --output text)"
echo "Rolling back the apply of ${from_commit}, by ${who}"

summary=()
for name in claude-assistant-policy claude-assistant-iam "$BOUNDARY"; do
  arn="$(arn_of "$name")"
  if ! aws iam get-policy --policy-arn "$arn" >/dev/null 2>&1; then summary+=("${name}: doesn't exist, nothing to do"); continue; fi
  versions="$(aws iam list-policy-versions --policy-arn "$arn" --output json)"
  current="$(jq -r '.Versions[] | select(.IsDefaultVersion) | .VersionId' <<<"$versions")"
  # The newest version created before the current default (ISO dates sort as text).
  previous="$(jq -r --arg cur "$current" '
    (.Versions[] | select(.VersionId == $cur) | .CreateDate) as $d
    | [.Versions[] | select(.CreateDate < $d)] | sort_by(.CreateDate) | last | .VersionId // empty' <<<"$versions")"
  if [[ -n "$previous" && "$previous" != None ]]; then
    aws iam set-default-policy-version --policy-arn "$arn" --version-id "$previous"
    summary+=("${name}: ${current} -> ${previous}")
  elif [[ "$name" == "$BOUNDARY" ]]; then
    summary+=("${name}: no previous version; left as is (a boundary only ever limits)")
  else
    aws iam detach-user-policy --user-name "$USER_NAME" --policy-arn "$arn" 2>/dev/null || true
    summary+=("${name}: no previous version; detached from Claude's user")
  fi
  # IAM tag values allow only letters, digits, spaces and _ . : / = + - @ (max 256).
  tag_who="$(LC_ALL=C sed 's#[^A-Za-z0-9 _.:/=+@-]# #g; s#  *# #g' <<<"${who:0:256}")"
  aws iam tag-policy --policy-arn "$arn" --tags Key=applied-commit,Value="rolled-back-from-${from_commit}" \
    Key=applied-at,Value="$(date -u +%Y-%m-%dT%H:%M:%SZ)" Key=applied-by,Value="$tag_who"
done
printf '%s\n' "${summary[@]}"

# GitHub: mark the last successful "aws" deployment inactive, and ask for a revert in git.
for id in $(gh api "repos/${REPO}/deployments?environment=aws&per_page=30" --jq '.[].id'); do
  state="$(gh api "repos/${REPO}/deployments/${id}/statuses?per_page=1" --jq '.[0].state')"
  if [[ "$state" == success ]]; then
    gh api "repos/${REPO}/deployments/${id}/statuses" -f state=inactive -f environment=aws \
      -f description="Rolled back $(date -u +%Y-%m-%dT%H:%MZ)" >/dev/null && echo "deployment ${id} marked inactive"
    break
  fi
done
body="$(printf 'The apply of `%s` was rolled back in AWS by %s.\n\n%s\n\nAWS no longer matches `main`. Revert the change in a PR (or fix it forward), merge, and apply, so `check-live.py` shows no drift again.' \
  "$from_commit" "$who" "$(printf -- '- %s\n' "${summary[@]}")")"
gh issue create --repo "$REPO" --title "Rolled back ${from_commit:0:12}: revert it in git" --body "$body" || echo "WARNING: couldn't open the issue"
