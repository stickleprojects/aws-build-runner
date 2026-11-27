#!/usr/bin/env bash
# Arm auto-merge on a pull request Claude just opened in this repo (#85), as the agent's App.
#
#   part1-godmode/arm-auto-merge.sh <PR number>
#
# Kieron's GitHub Approve click is the approval: once he approves the latest commit and the required checks
# (policy, approved) pass, GitHub merges it (squash). This only arms that. It refuses when:
#   - the session isn't the agent's App (SDLC_AGENT_IDENTITY unset): never arm it as Kieron
#   - the PR isn't open, or isn't this repo's
#   - GitHub doesn't require a review on it (reviewDecision null): then auto-merge would skip the person
# Run it straight after opening the PR: GitHub refuses once a PR is approved and green.
set -euo pipefail
pr="${1:-}"
[[ "$pr" =~ ^[0-9]+$ ]] || { echo "usage: $0 <PR number>" >&2; exit 1; }
[[ -n "${SDLC_AGENT_IDENTITY:-}" ]] || { echo "arm-auto-merge: not running as the agent's App; refusing" >&2; exit 1; }
repo=stickleprojects/aws-build-runner
# GraphQL by explicit query (gh pr view's defaults trip over the Projects (classic) deprecation).
# shellcheck disable=SC2016 # $o, $n, $p are GraphQL variables, not shell ones
info="$(gh api graphql -f query='query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){pullRequest(number:$p){id state reviewDecision autoMergeRequest{enabledAt}}}}' \
  -F o="${repo%/*}" -F n="${repo#*/}" -F p="$pr" --jq .data.repository.pullRequest)"
[[ "$(jq -r .state <<<"$info")" == OPEN ]] || { echo "arm-auto-merge: #$pr isn't open" >&2; exit 1; }
decision="$(jq -r '.reviewDecision // ""' <<<"$info")"
[[ -n "$decision" ]] || { echo "arm-auto-merge: #$pr doesn't require a review, so auto-merge would skip Kieron; not armed" >&2; exit 1; }
if [[ "$(jq -r '.autoMergeRequest.enabledAt // ""' <<<"$info")" != "" ]]; then
  echo "#$pr: auto-merge already armed; it merges once Kieron approves and the checks pass"; exit 0
fi
# shellcheck disable=SC2016 # $id is a GraphQL variable
gh api graphql -f query='mutation($id:ID!){enablePullRequestAutoMerge(input:{pullRequestId:$id,mergeMethod:SQUASH}){pullRequest{number}}}' \
  -F id="$(jq -r .id <<<"$info")" --jq '.data.enablePullRequestAutoMerge.pullRequest.number' >/dev/null
echo "#$pr: auto-merge armed (squash); it merges once Kieron approves the latest commit and policy and approved pass"
