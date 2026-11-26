#!/usr/bin/env bash
# Godmode, in the MANAGEMENT account (CloudShell): the organization's region lock (#30 fix 7, 2026-11-27).
#
#   cd ~/aws-build-runner && git pull && cd part1-godmode && ./bootstrap-org-region-lock.sh
#
# Creates or updates the SCP org-region-lock from bootstrap/org-region-lock-scp.json and attaches it to Root:
#   - no EC2 or SSM in us-east-1. The AWS-generated AdvancedModeRegionRestriction SCP allows EC2 there, and
#     nothing of ours runs there. us-east-1 stays open for the global services (IAM, Budgets, Cost Explorer,
#     billing, Organizations, support), which that SCP already limits it to.
#   - no Bedrock (AI model calls) anywhere: that SCP lets them through in every region; nobody uses it.
# A new SCP, not an edit to the AWS-generated one, which could be overwritten. Idempotent; ends with a check.
# The kill switch sweeps Sydney only once this is attached (bootstrap-budget-stop.sh, via the guardrails workflow).
set -euo pipefail
cd "$(dirname "$0")"

NAME=org-region-lock
FILE=bootstrap/org-region-lock-scp.json
master="$(aws organizations describe-organization --query Organization.MasterAccountId --output text)"
[[ "$(aws sts get-caller-identity --query Account --output text)" == "$master" ]] \
  || { echo "Refusing: run this in the organization's management account." >&2; exit 1; }
root="$(aws organizations list-roots --query 'Roots[0].Id' --output text)"

echo "== SCP ${NAME}"
id="$(aws organizations list-policies --filter SERVICE_CONTROL_POLICY \
  --query "Policies[?Name=='${NAME}'].Id | [0]" --output text)"
if [[ -z "$id" || "$id" == None ]]; then
  id="$(aws organizations create-policy --type SERVICE_CONTROL_POLICY --name "$NAME" \
    --description "No EC2/SSM in us-east-1, no Bedrock anywhere (aws-build-runner #30)" \
    --content "file://${FILE}" --query Policy.PolicySummary.Id --output text)"
  echo "created ${id}"
elif [[ "$(aws organizations describe-policy --policy-id "$id" --query Policy.Content --output text | jq -S -c .)" \
        == "$(jq -S -c . "$FILE")" ]]; then
  echo "unchanged (${id})"
else
  aws organizations update-policy --policy-id "$id" --content "file://${FILE}" >/dev/null
  echo "updated (${id})"
fi

echo "== attached to Root (${root})"
if aws organizations list-targets-for-policy --policy-id "$id" --query 'Targets[].TargetId' --output text | grep -qw "$root"; then
  echo "already"
else
  aws organizations attach-policy --policy-id "$id" --target-id "$root"
  echo "attached"
fi

echo "== check"
aws organizations describe-policy --policy-id "$id" --query Policy.Content --output text | jq -c '.Statement[] | {Sid, Action}'
aws organizations list-targets-for-policy --policy-id "$id" --query 'Targets[].Name' --output text
echo "Done. EC2 in us-east-1 and Bedrock are now refused for every account in the organization."
